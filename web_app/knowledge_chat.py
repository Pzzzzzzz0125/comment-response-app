"""Constrained conversational layer over the existing permit-history store."""

from __future__ import annotations

import re
import secrets
import time
from collections import Counter
from typing import Any, Callable

try:
    from .data_trust import verified_text
    from .progressive_retrieval import ValidatedTagIndex, progressive_retrieve, topic_from_query
except ImportError:
    from data_trust import verified_text
    from progressive_retrieval import ValidatedTagIndex, progressive_retrieve, topic_from_query


def _project_key(row: dict[str, Any]) -> str:
    """Use the normalized hierarchy identity for project counts/grouping."""
    return str(
        row.get("project_id")
        or row.get("project_name")
        or row.get("site_id")
        or row.get("property_project")
        or "unknown"
    ).strip()


def _project_label(row: dict[str, Any]) -> str:
    """Use one stable human-readable project label in chat evidence."""
    return str(
        row.get("project_name")
        or row.get("site_name")
        or row.get("property_project")
        or row.get("city")
        or "unknown"
    ).strip()


ALLOWED_INTENTS = {
    "precedent_search", "historical_response_summary", "aggregate_count",
    "topic_summary", "compare_groups", "filter_previous_results",
    "explain_selected_comment", "database_exploration", "unsupported_or_ambiguous",
}
ALLOWED_OPERATIONS = {
    "smart_search", "keyword_search", "count_parent_comments", "count_searchable_units",
    "count_projects", "count_review_rounds", "count_canonical_issues", "group_by_city",
    "group_by_discipline", "group_by_canonical_issue", "summarize_confirmed_responses",
    "load_previous_result_set", "load_filtered_comments", "group_by_project",
    "group_by_review_round", "group_by_response_status", "group_by_reviewer",
    "group_by_code_section", "group_by_comment_type",
}
ALLOWED_FILTERS = {
    "city", "site_id", "project_id", "discipline", "review_round",
    "category", "response_status",
}
GUIDED_ACTION_TYPES = {
    "filter_subtopic", "compare_projects", "timeline_analysis",
    "response_analysis", "unresolved_analysis", "broaden_scope",
}
GENERIC_QUERY_WORDS = {
    "a", "about", "all", "and", "are", "comment", "comments", "concern", "concerning",
    "count", "did", "do", "does", "find", "handled", "have", "has", "historical", "history",
    "i", "me", "we", "you", "was", "were", "been", "can", "could", "would", "should",
    "how", "in", "involving", "many", "of", "our", "permit", "please", "previously",
    "mention", "mentioned", "mentioning", "record", "records", "response", "responses", "show", "summarize", "the", "there",
    "these", "those", "to", "what", "with", "across", "between", "versus", "vs", "compare", "different",
    "first", "second", "most", "least", "more", "less", "than", "usually", "typically", "often", "project", "projects",
    "which", "received", "contains", "review", "round", "city", "cities", "related", "confirmed", "examples", "example",
    "three", "resolutions", "resolution", "later", "reviewers", "reviewer", "new", "team", "learn", "recurring", "historically",
    "from", "for", "by", "or", "as", "at", "on", "issue", "issues", "plan", "plans",
}
TERM_EQUIVALENTS = {
    "size": {"size", "dimension", "width"},
    "dimension": {"dimension", "size", "width"},
    "width": {"width", "dimension", "size"},
    "protection": {"protection", "protect", "removal", "arborist", "fencing"},
    "protect": {"protection", "protect", "removal", "arborist", "fencing"},
    "rating": {"rating", "rated"},
    "rated": {"rating", "rated"},
}

# These aliases are intentionally small and conservative.  They are used only
# as a sanity check after retrieval; Gemini remains responsible for semantic
# ranking, but an obviously unrelated result must not become the answer to a
# narrow question (for example, beam sizing for a drainage question).
TOPIC_ALIASES = {
    "drainage": {"drainage", "drain", "runoff", "stormwater", "swale", "infiltration", "detention", "downspout", "grading", "slope", "discharge"},
    "fire": {"fire", "rated", "rating", "separation", "assembly", "gypsum", "ul", "crc", "r302"},
    "tree": {"tree", "trees", "arborist", "root", "canopy", "removal", "preservation", "fencing"},
    "door": {"door", "doors", "opening", "openings", "width", "height", "dimension", "size"},
    "beam": {"beam", "beams", "framing", "joist", "structural", "load", "header", "girder"},
}

# A narrow, controlled vocabulary is used as a hard safety gate before a
# conversational answer is generated.  Retrieval can surface a useful
# neighbour, but it must not turn a grading/drainage record into evidence for
# a fire-separation comparison.
CONTROLLED_TOPIC_TERMS = {
    "fire_separation": {
        "fire separation", "fire-resistance-rated", "fire rated wall",
        "one-hour wall", "1-hour wall", "property line wall",
        "opening protection", "protected opening", "exterior wall rating",
        "eave projection", "garage separation", "dwelling unit separation",
        "rated assembly", "penetration protection", "fire", "rated",
        "rating", "separation", "assembly", "gypsum", "sprinkler",
        "garage", "dwelling", "property", "opening", "eave",
        "penetration", "r302", "ul", "crc",
    },
    "drainage": {
        "drainage", "drain", "runoff", "stormwater", "swale",
        "infiltration", "detention", "downspout", "grading", "slope",
        "discharge",
    },
    "tree_protection": {
        "tree", "arborist", "root", "canopy", "removal", "preservation",
        "tree protection", "heritage tree",
    },
    "tree_related": {
        "tree", "trees", "arborist", "root", "canopy", "removal",
        "preservation", "inventory", "heritage tree",
    },
    "door_size": {
        "door size", "door width", "door height", "door dimension",
        "clear width", "clearance width", "door opening width",
    },
    "door_rating": {
        "door rating", "rated door", "fire-rated door", "fire rated door",
        "door assembly", "door label", "door fire rating",
    },
    "door_attributes": {
        "door size", "door width", "door height", "door dimension",
        "clear width", "clearance width", "door rating", "rated door",
        "fire-rated door", "fire rated door", "door assembly", "door label",
    },
}


def _controlled_topic(subject: str) -> str | None:
    """Map a user subject to a safety-sensitive topic, when possible."""
    lower = str(subject or "").casefold().replace("-", " ")
    if any(phrase in lower for phrase in ("fire separation", "fire rated", "fire rating", "fire resistance", "fire wall")):
        return "fire_separation"
    if any(token in lower.split() for token in ("fire", "rated", "separation", "eave", "garage")):
        # A standalone ``separation``/``garage`` query can still be about
        # fire separation, but a plain ``fire`` query remains intentionally
        # broad and is handled by the ordinary topic guard.
        if "fire" in lower or "separation" in lower:
            return "fire_separation"
    if any(token in lower.split() for token in ("drainage", "drain", "runoff", "stormwater", "grading")):
        return "drainage"
    if any(token in lower.split() for token in ("tree", "trees", "arborist", "heritage")):
        if re.search(r"\b(?:protect|protection|impact|root|preserv)\w*\b", lower):
            return "tree_protection"
        return "tree_related"
    # Keep size and fire-rating questions separate.  A combined comparison
    # deliberately uses a third topic so the validator does not discard one
    # side of the comparison before the answer can count each group.
    has_door = bool(re.search(r"\bdoors?\b", lower))
    has_size = bool(re.search(r"\b(?:size|width|height|dimension|clear(?:ance)?\s+width|narrow)\b", lower))
    has_rating = bool(re.search(r"\b(?:rating|rated|fire[- ]?rated|assembly|label)\b", lower))
    if has_door and has_size and has_rating:
        return "door_attributes"
    if has_door and has_size:
        return "door_size"
    if has_door and has_rating:
        return "door_rating"
    return None


class PlanValidationError(ValueError):
    pass


def _clean_text(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _complete_excerpt(value: Any, limit: int = 500) -> tuple[str, bool]:
    """Return a bounded excerpt without presenting a cut word as source text.

    ``_clean_text`` is intentionally a generic hard bound and is used for IDs,
    labels, and other non-prose values.  Evidence prose needs a different
    contract: a character limit must never turn ``ordinance-size tree`` into
    the apparently factual fragment ``ordinance-size t``.  Prefer the last
    complete sentence inside the bound; otherwise use a visible ellipsis at a
    word boundary and mark the excerpt incomplete so synthesis cannot quote it.
    """
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(clean) <= limit:
        return clean, True
    bounded = clean[:limit]
    sentence_ends = [match.end() for match in re.finditer(r"[.!?](?=\s|$)", bounded)]
    if sentence_ends and sentence_ends[-1] >= max(30, limit // 4):
        # The source contains more text, but this excerpt itself is a complete
        # sentence and is therefore safe for synthesis to quote or paraphrase.
        return bounded[:sentence_ends[-1]].strip(), True
    word_boundary = bounded.rfind(" ")
    if word_boundary >= max(40, limit // 3):
        return bounded[:word_boundary].rstrip(" ,;:-") + "…", False
    return "", False


def _term_root(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _safe_scope(value: Any) -> dict[str, Any]:
    """Keep Query Plan scope metadata bounded and non-executable."""
    if not isinstance(value, dict):
        return {}
    allowed = {"city_ids", "site_ids", "project_ids", "date_range", "review_rounds"}
    scope: dict[str, Any] = {}
    for key in allowed:
        raw = value.get(key)
        if key in {"city_ids", "site_ids", "project_ids", "review_rounds"}:
            if isinstance(raw, (list, tuple)):
                items = [_clean_text(item, 120) for item in raw if _clean_text(item, 120)]
                if items:
                    scope[key] = items[:100]
        elif isinstance(raw, dict):
            safe_range = {
                bound: _clean_text(raw.get(bound), 40)
                for bound in ("from", "to")
                if _clean_text(raw.get(bound), 40)
            }
            if safe_range:
                scope[key] = safe_range
    return scope


def _significant_terms(value: str) -> set[str]:
    terms: set[str] = set()
    for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", str(value or "").casefold()):
        for part in word.split("-"):
            if part not in GENERIC_QUERY_WORDS and len(part) > 1:
                terms.add(_term_root(part))
    return terms


def _topic_terms(value: str) -> set[str]:
    terms = _significant_terms(value)
    expanded = set(terms)
    for key, aliases in TOPIC_ALIASES.items():
        if key in terms or terms & aliases:
            expanded.update(aliases)
    for term in list(terms):
        expanded.update(TERM_EQUIVALENTS.get(term, {term}))
    return expanded


def validate_query_plan(raw: Any) -> dict[str, Any]:
    """Return a safe allowlisted plan; arbitrary SQL/tool instructions are rejected."""
    if not isinstance(raw, dict):
        raise PlanValidationError("Gemini query plan must be an object")
    intent = _clean_text(raw.get("intent"), 80)
    if intent not in ALLOWED_INTENTS:
        raise PlanValidationError("Query plan contains an unsupported intent")
    operations = raw.get("operations", [])
    if not isinstance(operations, list) or not all(isinstance(item, str) for item in operations):
        raise PlanValidationError("Query plan operations must be a list of strings")
    if any(item not in ALLOWED_OPERATIONS for item in operations):
        raise PlanValidationError("Query plan contains a non-allowlisted operation")
    serialized = str(raw).casefold()
    if re.search(r"\b(select|insert|update|delete|drop|alter|pragma|attach)\b.+\b(from|into|table|database)\b", serialized):
        raise PlanValidationError("SQL is not permitted in a knowledge query plan")
    filters = raw.get("filters", {})
    if not isinstance(filters, dict):
        raise PlanValidationError("Query plan filters must be an object")
    safe_filters = {
        key: _clean_text(value, 120)
        for key, value in filters.items()
        if key in ALLOWED_FILTERS and _clean_text(value, 120)
    }
    # Keep the model-facing planner allowlisted, but preserve the structured
    # fields used by progressive retrieval when a client provides them.  They
    # are metadata only; retrieval still recomputes the actual topic locally.
    primary_topics = raw.get("primary_topics", [])
    objects = raw.get("objects", [])
    response_requirements = raw.get("response_requirements", {})
    safe_primary_topics = [
        _clean_text(item, 80) for item in primary_topics
        if isinstance(item, str) and _clean_text(item, 80)
    ][:8] if isinstance(primary_topics, list) else []
    safe_objects = [
        _clean_text(item, 80) for item in objects
        if isinstance(item, str) and _clean_text(item, 80)
    ][:12] if isinstance(objects, list) else []
    safe_requirements = {
        key: bool(response_requirements.get(key))
        for key in (
            "confirmed_responses_required",
            "comparison_required",
            "timeline_required",
        )
    } if isinstance(response_requirements, dict) else {}
    return {
        "intent": intent,
        "subject": _clean_text(raw.get("subject"), 500),
        "operations": list(dict.fromkeys(operations)),
        "filters": safe_filters,
        "needs_clarification": bool(raw.get("needs_clarification")),
        "clarification_question": _clean_text(raw.get("clarification_question"), 300),
        "raw_query": _clean_text(raw.get("raw_query"), 4000),
        "primary_topics": safe_primary_topics,
        "objects": safe_objects,
        "response_requirements": safe_requirements,
        "scope": _safe_scope(raw.get("scope")),
    }


def enrich_query_plan(plan: dict[str, Any], message: str, has_previous: bool = False, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach a deterministic, spec-shaped query plan to any safe plan.

    The planner is deliberately local-first.  A model may suggest an intent,
    but topic, objects, response requirements, and scope are reconstructed
    from the user message and explicit filters before retrieval.  This keeps
    the plan useful for telemetry without allowing a stale/incorrect model
    topic to move the evidence boundary.
    """
    value = dict(plan)
    raw_query = _clean_text(message, 4000)
    subject = _clean_text(value.get("subject") or raw_query, 500)
    intent = str(value.get("intent") or "precedent_search")
    topic = topic_from_query(subject) or topic_from_query(raw_query) or _controlled_topic(subject)
    object_aliases = {
        "tree_protection": ["tree", "root_zone", "arborist"],
        "tree_related": ["tree", "arborist"],
        "fire_separation": ["rated_wall", "opening_protection", "dwelling_unit_separation"],
        "drainage": ["grading", "runoff", "stormwater"],
        "door_size": ["door", "opening", "clear_width"],
        "door_rating": ["door", "rated_assembly", "opening_protection"],
        "structural_calculations": ["framing", "foundation", "load_path"],
    }
    topics = [topic] if topic else []
    objects = list(object_aliases.get(topic or "", []))
    if not objects:
        words = [
            _term_root(word) for word in re.findall(r"[a-z0-9]+", subject.casefold())
            if word not in GENERIC_QUERY_WORDS and len(word) > 2
        ]
        objects = list(dict.fromkeys(words[:8]))
    requirements = {
        "confirmed_responses_required": intent in {"historical_response_summary", "response_analysis"},
        "comparison_required": intent in {"compare_groups", "comparison", "compare"},
        "timeline_required": intent in {"timeline", "timeline_analysis"},
    }
    mode_by_intent = {
        "precedent_search": "LOOKUP",
        "aggregate_count": "COUNT",
        "topic_summary": "SUMMARY",
        "historical_response_summary": "SUMMARY",
        "compare_groups": "COMPARISON",
        "timeline_analysis": "TIMELINE",
        "filter_previous_results": "FOLLOW_UP",
    }
    raw_scope = dict(scope or value.get("scope") or {})
    if "city" in raw_scope and "city_ids" not in raw_scope:
        raw_scope["city_ids"] = [raw_scope.pop("city")]
    if "project" in raw_scope and "project_ids" not in raw_scope:
        raw_scope["project_ids"] = [raw_scope.pop("project")]
    if "project_id" in raw_scope and "project_ids" not in raw_scope:
        raw_scope["project_ids"] = [raw_scope.pop("project_id")]
    if "site_id" in raw_scope and "site_ids" not in raw_scope:
        raw_scope["site_ids"] = [raw_scope.pop("site_id")]
    if "review_round" in raw_scope and "review_rounds" not in raw_scope:
        raw_scope["review_rounds"] = [raw_scope.pop("review_round")]
    value.update({
        "raw_query": raw_query,
        "subject": subject,
        "primary_topics": topics,
        "objects": objects,
        "response_requirements": requirements,
        "scope": _safe_scope(raw_scope),
        "mode": mode_by_intent.get(intent, "LOOKUP"),
        "has_previous_result_set": bool(has_previous),
    })
    return value


def fallback_query_plan(message: str, has_previous: bool) -> dict[str, Any]:
    """Conservative local router used only when Gemini routing is unavailable."""
    lower = message.casefold()
    # ``only`` inside a standalone question ("only state that they would") is
    # not a conversation reference.  Treat it as a follow-up only when the
    # wording actually points at an earlier result set or when a prior set is
    # available to filter.
    followup_reference = re.search(
        r"^(?:only|show|summarize)\s+(?:those|these|them|the same)|\bwithout\s+responses?\b|\bhow did we respond\b",
        lower,
    )
    # A concrete noun makes an otherwise broad comparison self-contained:
    # “Compare these issues across projects” should be routed as a comparison,
    # not treated as a dangling follow-up when no prior result set exists.
    if has_previous and re.search(r"\b(?:those|these|them)\b", lower):
        followup_reference = True
    if re.search(r"\b(?:explain|describe)\b.*\b(?:this|selected)\s+comment\b", lower):
        intent, operations = "explain_selected_comment", []
    elif followup_reference:
        intent, operations = "filter_previous_results", ["load_previous_result_set"]
    elif re.search(r"\bhow many\b|\bcount\b|\bnumber of\b", lower):
        intent = "aggregate_count"
        operations = ["keyword_search", "count_parent_comments", "count_projects", "count_review_rounds"]
    elif re.search(r"\bwhich\s+(?:review\s+round|city)\b", lower):
        intent = "aggregate_count"
        operations = ["keyword_search", "count_parent_comments", "count_projects", "count_review_rounds"]
    elif re.search(r"\bcompare\b|\bdifferences?\b", lower):
        intent, operations = "compare_groups", ["smart_search", "group_by_city", "summarize_confirmed_responses"]
    elif re.search(r"\bsummar|\boverview|\bbreakdown|\bdistribution", lower):
        intent, operations = "topic_summary", ["load_filtered_comments", "group_by_discipline", "group_by_response_status", "summarize_confirmed_responses"]
    elif re.search(r"\b(?:what should|what mistakes|learn from|avoid|based on past)\b", lower):
        intent, operations = "historical_response_summary", ["smart_search", "summarize_confirmed_responses"]
    elif re.search(r"\b(?:later confirmed|confirmed by reviewers?|reviewers? confirmed)\b", lower):
        intent, operations = "historical_response_summary", ["smart_search", "summarize_confirmed_responses"]
    elif re.search(r"\brespond|\bhandled|\bapplicant|\bapplicants|\brevise\b|\bstate\b", lower):
        intent, operations = "historical_response_summary", ["smart_search", "summarize_confirmed_responses"]
    else:
        intent, operations = "precedent_search", ["smart_search"]
    subject_words = [
        word for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", lower)
        if word not in GENERIC_QUERY_WORDS
    ]
    if not subject_words and re.search(r"\b(?:applicant|applicants|revise|response|respond|handled)\b", lower):
        subject_words = ["applicant", "responses", "plan", "revisions"]
    ambiguous_response_question = bool(
        re.search(r"\bapplicants?\b.*\b(?:revise|changed|updated)\b.*\b(?:plans?|state|would)\b", lower)
        and not (_controlled_topic(lower) or re.search(r"\b(?:tree|drainage|fire|door|setback|grading|accessibility)\b", lower))
    )
    ambiguous_timeline_question = bool(
        re.search(r"\b(?:trace|follow|history|unresolved)\b", lower)
        and re.search(r"\b(?:issue|comment|comments?)\b", lower)
        and not (_controlled_topic(lower) or re.search(r"\b(?:tree|drainage|fire|door|setback|grading|accessibility|wall|roof|garage)\b", lower))
    )
    ambiguous_comparison = bool(
        intent == "compare_groups"
        and not subject_words
        and not _controlled_topic(lower)
        and not has_previous
    )
    ambiguous_topic_question = ""
    if re.search(r"\bseparation issues?\b", lower) and not re.search(r"\b(?:fire|dwelling[- ]unit|property[- ]line)\s+separation", lower):
        ambiguous_topic_question = "Do you mean fire separation, dwelling-unit separation, or property-line separation?"
    elif not _controlled_topic(lower):
        if re.search(r"\bwall\w*\b", lower) and not re.search(r"\b(?:fire|structural|bearing|retaining|property[- ]line)\s+wall", lower):
            ambiguous_topic_question = "Do you mean structural walls, fire-rated walls, or property-line walls?"
        elif re.search(r"\baccess problems?\b", lower):
            ambiguous_topic_question = "Do you mean accessibility clearances, site access, or building-entry access?"
        elif re.search(r"\bissues?\b.*\bdoors?\b|\bdoors?\b.*\bissues?\b", lower):
            ambiguous_topic_question = "Should I focus on door size, door ratings, hardware, or another door requirement?"
        elif re.search(r"\bsite problems?\b", lower):
            ambiguous_topic_question = "Should I focus on grading, drainage, access, or another site issue?"
    return validate_query_plan({
        "intent": intent,
        "subject": " ".join(subject_words) or _clean_text(message),
        "operations": operations,
        "filters": {},
        "needs_clarification": bool((followup_reference and not has_previous) or ambiguous_response_question or ambiguous_timeline_question or ambiguous_comparison or ambiguous_topic_question),
        "clarification_question": (
            "I do not have a previous verified result set to filter. What topic should I search first?"
            if followup_reference and not has_previous
            else "Which issue or topic should I use to compare applicant statements with documented plan revisions?"
            if ambiguous_response_question
            else "Which specific issue or topic should I trace across review rounds?"
            if ambiguous_timeline_question
            else "Which topic or projects should I compare?"
            if ambiguous_comparison
            else ambiguous_topic_question
            if ambiguous_topic_question else ""
        ),
    })


class KnowledgeChat:
    """Execute constrained plans and retain short-lived, verified result sets."""

    def __init__(self, store: Any, ttl_seconds: int = 1800, clock: Callable[[], float] = time.time):
        self.store = store
        self.ttl_seconds = max(60, ttl_seconds)
        self.clock = clock
        self.result_sets: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.remote_circuit_until = 0.0
        self._tag_index: ValidatedTagIndex | None = None
        self._tag_index_signature: tuple[int, int] | None = None
        self._last_progressive_result: dict[str, Any] = {}

    def _progressive_index(self) -> ValidatedTagIndex:
        """Build the tag projection only when the canonical row set changes."""
        overlay = getattr(self.store, "_tag_overlaid_rows", None)
        rows = overlay() if callable(overlay) else getattr(self.store, "_comments", [])
        signature = (
            len(rows),
            hash(tuple(
                (
                    str(row.get("comment_id", "")),
                    repr(row.get("event_tags", [])),
                    repr(row.get("issue_tags", [])),
                )
                for row in rows
            )),
        )
        if self._tag_index is None or self._tag_index_signature != signature:
            self._tag_index = ValidatedTagIndex(rows)
            self._tag_index_signature = signature
        return self._tag_index

    def _chat_client(self) -> Any | None:
        """Return the chat client unless a recent remote failure opened the circuit."""
        if time.monotonic() < self.remote_circuit_until:
            return None
        return self.store.knowledge_gemini_client or self.store.gemini_client

    def _mark_remote_failure(self) -> None:
        # A short circuit prevents a burst of follow-up questions from each
        # repeating the same timeout/429.  Retrieval and deterministic answers
        # continue to work while the remote service recovers.
        self.remote_circuit_until = time.monotonic() + 60.0

    @staticmethod
    def _verification_failure_kind(exc: Exception) -> str:
        """Return a safe, user-facing class for a Gemini verification error."""
        detail = str(exc).casefold()
        if any(token in detail for token in (
            "credits", "rate limit", "resource_exhausted", "spending cap", "http 429",
        )):
            return "capacity"
        if any(token in detail for token in (
            "timed out", "timeout", "urlopen error", "name or service not known",
            "nodename nor servname", "connection", "network",
        )):
            return "network"
        if any(token in detail for token in (
            "not found", "unsupported", "unknown model", "http 404",
        )):
            return "model"
        if any(token in detail for token in (
            "structured", "missing candidate", "omitted candidate", "no valid",
        )):
            return "incomplete"
        return "unknown"

    @staticmethod
    def _verification_batches(
        candidates: list[dict[str, Any]],
        *,
        maximum_records: int = 6,
        maximum_characters: int = 24_000,
    ) -> list[list[dict[str, Any]]]:
        """Create bounded validation packets without truncating evidence text."""
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_size = 0
        for candidate in candidates:
            candidate_size = sum(
                len(str(candidate.get(key, "")))
                for key in ("comment_text", "response_text", "project", "discipline")
            )
            if current and (
                len(current) >= maximum_records
                or current_size + candidate_size > maximum_characters
            ):
                batches.append(current)
                current = []
                current_size = 0
            current.append(candidate)
            current_size += candidate_size
        if current:
            batches.append(current)
        return batches

    def _verify_candidate_batch(
        self,
        client: Any,
        subject: str,
        batch: list[dict[str, Any]],
        *,
        split_depth: int = 0,
    ) -> list[dict[str, Any]]:
        """Verify every ID, retrying an incomplete/oversized packet safely.

        Gemini occasionally returns valid decisions for only part of a large
        structured request.  Treating that as success loses coverage; treating
        it as a total failure discards already useful work.  Retry only the
        missing subset, split it when necessary, and require exactly one final
        decision for every supplied ID before any record becomes evidence.
        """
        if not batch:
            return []
        expected = {str(item.get("candidate_id", "")) for item in batch}
        try:
            if hasattr(client, "validate_knowledge_evidence"):
                decisions = client.validate_knowledge_evidence(subject, batch)
            elif hasattr(client, "verify_knowledge_topic"):
                decisions = [{
                    "candidate_id": item.get("candidate_id"),
                    "is_relevant": item.get("match_class") == "direct",
                    "matched_concept": subject,
                    "supporting_excerpt": "",
                    "confidence": float(item.get("confidence", 0) or 0),
                    "exclude_reason": "Evidence is adjacent to, but does not directly answer, the requested topic.",
                } for item in client.verify_knowledge_topic(subject, batch)]
            else:
                decisions = []
        except (RuntimeError, ValueError, TypeError) as exc:
            # Do not multiply requests after an explicit capacity/credit
            # refusal.  Transient/size-related failures may recover when the
            # evidence packet is divided, but recursion is deliberately
            # bounded so one question cannot retry forever.
            if (
                self._verification_failure_kind(exc) == "capacity"
                or len(batch) == 1
                or split_depth >= 3
            ):
                raise
            middle = max(1, len(batch) // 2)
            return (
                self._verify_candidate_batch(client, subject, batch[:middle], split_depth=split_depth + 1)
                + self._verify_candidate_batch(client, subject, batch[middle:], split_depth=split_depth + 1)
            )

        valid: dict[str, dict[str, Any]] = {}
        for item in decisions if isinstance(decisions, list) else []:
            candidate_id = str(item.get("candidate_id", "")) if isinstance(item, dict) else ""
            if candidate_id in expected and candidate_id not in valid:
                valid[candidate_id] = item
        missing_ids = expected - set(valid)
        if missing_ids:
            missing = [
                item for item in batch
                if str(item.get("candidate_id", "")) in missing_ids
            ]
            if len(batch) == 1 or split_depth >= 3:
                raise RuntimeError(
                    f"Gemini structured evidence verification omitted candidate {next(iter(missing_ids))}"
                )
            recovered = self._verify_candidate_batch(
                client,
                subject,
                missing,
                split_depth=split_depth + 1,
            )
            for item in recovered:
                valid[str(item.get("candidate_id", ""))] = item
        if expected != set(valid):
            raise RuntimeError("Gemini structured evidence verification returned incomplete coverage")
        return [valid[str(item.get("candidate_id", ""))] for item in batch]

    def _purge(self) -> None:
        now = self.clock()
        self.result_sets = {
            key: value for key, value in self.result_sets.items()
            if float(value["expires_at"]) > now
        }

    def _route(self, message: str, has_previous: bool) -> tuple[dict[str, Any], list[str]]:
        # Routing is deterministic for every question the local allowlist can
        # understand.  Calling Gemini before retrieval used to add one network
        # round trip (and up to five retries) to every chat message, even for
        # straightforward questions such as "tree protection".  Keep Gemini
        # only as a bounded ambiguity resolver.
        local_plan = fallback_query_plan(message, has_previous)
        warnings: list[str] = []
        client = self._chat_client()
        # Aggregate questions may use an allowlisted planner when one is
        # configured. The planner receives only the user's question; all
        # evidence retrieval remains local-first after this bounded call.
        should_resolve_plan = bool(local_plan["needs_clarification"]) or (
            local_plan.get("intent") == "aggregate_count"
            and client is not None
            and hasattr(client, "plan_knowledge_query")
        )
        if not should_resolve_plan:
            return enrich_query_plan(local_plan, message, has_previous), []
        if client and hasattr(client, "plan_knowledge_query"):
            try:
                return enrich_query_plan(
                    validate_query_plan(client.plan_knowledge_query(message, has_previous)),
                    message,
                    has_previous,
                ), warnings
            except (RuntimeError, ValueError) as exc:
                self._mark_remote_failure()
                warnings.append(f"Gemini query routing was unavailable: {exc}")
        else:
            warnings.append("Gemini query routing is unavailable; a conservative local intent router was used.")
        return enrich_query_plan(local_plan, message, has_previous), warnings

    def _record_matches_filters(self, row: dict[str, Any], filters: dict[str, str]) -> bool:
        # Chat is an evidence layer, not a QA view.  Explicitly quarantined,
        # superseded, or needs-review rows must never become factual chat
        # evidence.  New canonical rows carry an explicit eligibility marker;
        # require all of the confirmation fields for those rows.  The small
        # legacy fixtures used by the unit tests (and old imports that have no
        # evidence metadata at all) remain readable until they are migrated.
        if not self._chat_evidence_eligible(row):
            return False
        for key in ("city", "site_id", "project_id", "discipline", "review_round", "category"):
            value = filters.get(key, "")
            if key == "category":
                actual = self.store._assignments.get(row["comment_id"], "Uncategorized")
            elif key == "project_id":
                actual = _project_key(row)
            else:
                actual = row.get(key, "")
            if value and str(actual).casefold() != value.casefold():
                return False
        status = filters.get("response_status", "")
        confirmed = self._confirmed_response(row["comment_id"])
        if status == "confirmed" and not confirmed:
            return False
        if status == "missing" and row.get("response_id"):
            return False
        if status == "unconfirmed" and (not row.get("response_id") or confirmed):
            return False
        return True

    @staticmethod
    def _chat_evidence_eligible(row: dict[str, Any]) -> bool:
        """Return whether a row may enter normal user-facing chat evidence.

        The normalized ingestion projection intentionally has a hard gate:
        only an explicitly confirmed, searchable event is authoritative.  A
        row with no evidence metadata is treated as a legacy row for backwards
        compatibility with old test/import fixtures; it is not confused with
        a row that explicitly failed verification.
        """
        verification_status = str(row.get("verification_status", "")).casefold().strip()
        trust_status = str(row.get("text_trust_status", "")).casefold().strip()
        # ``canonical_event_id`` is also added by the server's in-memory
        # projection for legacy fixtures.  It is not, by itself, proof that
        # the row passed the evidence gate; require explicit quality fields.
        has_normalized_metadata = bool(
            "verification_status" in row
            or "search_eligible" in row
            or "text_trust_status" in row
        )
        if not has_normalized_metadata:
            return True
        if verification_status != "confirmed":
            return False
        if row.get("search_eligible") is not True:
            return False
        if trust_status and trust_status != "verified":
            return False
        if row.get("verification_conflict"):
            return False
        return True

    def _validated_model_filters(self, filters: dict[str, str]) -> dict[str, str]:
        known = {
            "city": {str(row.get("city", "")).casefold() for row in self.store._comments},
            "discipline": {str(row.get("discipline", "")).casefold() for row in self.store._comments},
            "review_round": {str(row.get("review_round", "")).casefold() for row in self.store._comments},
            "category": {str(value).casefold() for value in self.store._assignments.values()} | {"uncategorized"},
        }
        return {
            key: value for key, value in filters.items()
            if key == "response_status" or (key in known and value.casefold() in known[key])
        }

    def _confirmed_response(self, comment_id: str) -> dict[str, Any] | None:
        link = self.store._links_by_comment.get(comment_id, {})
        if self.store._effective_link_status(link) != "confirmed":
            return None
        response = self.store._responses_by_id.get(str(link.get("response_id", "")))
        return response if response else None

    def _scope_overview(self, message: str, plan: dict[str, Any]) -> bool:
        if "load_filtered_comments" in plan["operations"] or plan["intent"] == "database_exploration":
            return True
        lower = message.casefold()
        if not re.search(r"\b(summary|summarize|overview|breakdown|distribution|list|which|what|response rate)\b", lower):
            return False
        ignored = set(GENERIC_QUERY_WORDS) | {
            "summary", "overview", "breakdown", "distribution", "overall", "city", "cities",
            "list", "which", "project", "projects", "discipline", "disciplines", "round", "rounds",
            "reviewer", "reviewers", "code", "codes", "status", "rate",
        }
        for city in self.store.cities():
            ignored.update(re.findall(r"[a-z0-9]+", city["name"].casefold()))
        subject_tokens = set(re.findall(r"[a-z0-9]+", plan["subject"].casefold()))
        return not (subject_tokens - ignored)

    def _breakdowns(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        response_status = Counter()
        for row in rows:
            if self._confirmed_response(row["comment_id"]):
                response_status["confirmed"] += 1
            elif row.get("response_id"):
                response_status["unconfirmed"] += 1
            else:
                response_status["missing"] += 1
        codes: Counter[str] = Counter()
        for row in rows:
            codes.update(re.findall(r"\b[A-Z]{2,6}\s+[A-Z]?\d+(?:\.\d+)+\b", verified_text(row)))
        values = {
            "cities": Counter(str(row.get("city", "unknown")) for row in rows),
            "disciplines": Counter(str(row.get("discipline", "unknown")) for row in rows),
            "projects": Counter(_project_label(row) for row in rows),
            "review_rounds": Counter(str(row.get("review_round", "unknown")) for row in rows),
            "reviewers": Counter(str(row.get("reviewer") or "not recorded") for row in rows),
            "response_status": response_status,
            "code_sections": codes,
        }
        return {
            key: dict(sorted(counter.items(), key=lambda item: (-item[1], item[0].casefold()))[:12])
            for key, counter in values.items()
        }

    def _keyword_ids(self, subject: str, filters: dict[str, str]) -> list[str]:
        # Narrow topics use the same audited gate as semantic retrieval.  This
        # prevents broad lexical matches (for example, a bird-safe window
        # record that merely says ``doors`` and ``same size``) from inflating a
        # door-size count.
        controlled = _controlled_topic(subject)
        if controlled in {"door_size", "door_rating", "door_attributes"}:
            ids: list[str] = []
            for comment in self.store._comments:
                if not self._record_matches_filters(comment, filters):
                    continue
                if self._local_relevance(subject, comment)["is_relevant"]:
                    ids.append(comment["comment_id"])
            return ids
        terms = list(dict.fromkeys(
            _term_root(part)
            for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", subject.casefold())
            for part in word.split("-")
            if part not in GENERIC_QUERY_WORDS and len(part) > 1
        ))
        if "tree" in terms and any(term in {"protection", "protect"} for term in terms):
            terms = [term for term in terms if term not in {"protection", "protect"}]
        if not terms:
            return []
        rows: list[tuple[int, str]] = []
        for comment in self.store._comments:
            if not self._record_matches_filters(comment, filters):
                continue
            haystack = " ".join([
                verified_text(comment), str(comment.get("discipline", "")),
                self.store._assignments.get(comment["comment_id"], ""),
            ]).casefold()
            haystack_terms = {_term_root(word) for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", haystack)}
            matched = sum(bool((TERM_EQUIVALENTS.get(term) or {term}) & haystack_terms) for term in terms)
            if matched == len(terms):
                rows.append((matched, comment["comment_id"]))
        rows.sort(key=lambda item: (-item[0], item[1]))
        return [comment_id for _, comment_id in rows]

    def _chat_verify_literal_ids(self, subject: str, comment_ids: list[str]) -> tuple[list[str], dict[str, str]]:
        client = self.store.knowledge_gemini_client or self.store.gemini_client
        if not client or not hasattr(client, "verify_knowledge_topic") or not comment_ids:
            return [], {}
        candidates = [{
            "candidate_id": comment_id,
            "city": self.store._comments_by_id[comment_id].get("city", ""),
            "discipline": self.store._comments_by_id[comment_id].get("discipline", ""),
            "comment_text": verified_text(self.store._comments_by_id[comment_id]),
        } for comment_id in comment_ids[:40]]
        try:
            verified = client.verify_knowledge_topic(subject, candidates)
        except RuntimeError:
            return [], {}
        allowed = set(comment_ids)
        classes = {
            str(item.get("candidate_id", "")): str(item.get("match_class", ""))
            for item in verified
            if str(item.get("candidate_id", "")) in allowed and item.get("match_class") in {"direct", "related"}
        }
        return [item for item in comment_ids if item in classes], classes

    def _candidate_matches_subject(self, subject: str, row: dict[str, Any]) -> bool:
        """Reject a semantic result with no observable connection to the question.

        This is deliberately a guardrail, not a replacement for semantic
        retrieval.  It catches the high-cost failure mode where a good answer
        is generated from a completely different topic.
        """
        terms = _topic_terms(subject)
        if not terms:
            return True
        haystack = " ".join((
            verified_text(row),
            str(row.get("discipline", "")),
            str(row.get("category", "")),
        )).casefold()
        haystack_terms = {
            _term_root(word)
            for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", haystack)
        }
        return bool(terms & haystack_terms)

    @staticmethod
    def _door_attribute_signals(text: str) -> dict[str, bool]:
        """Detect explicit door attributes without treating every door mention as evidence.

        A lexical ``door`` hit is too broad for questions such as ``door size``:
        bird-safe glazing, door hardware, outlets near a door, and egress prose
        often contain the word without discussing a dimension or rating.  The
        patterns below require an attribute phrase or a measurement tied to a
        door/opening.  They are intentionally auditable and conservative.
        """
        lower = str(text or "").casefold().replace("–", "-").replace("—", "-")
        size = bool(re.search(
            r"(?:\bdoors?\s+(?:size|width|height|dimension|opening\s+width|clear(?:ance)?\s+width)\b)"
            r"|(?:\b(?:size|width|height|dimension|clear(?:ance)?\s+width)\s+(?:of|for)\s+(?:the\s+)?doors?\b)"
            r"|(?:\b(?:doors?|openings?)\b[^.\n]{0,70}\b\d+(?:\.\d+)?\s*(?:inches?|in\\.|\"|feet|ft|')\s*(?:wide|high|width|height)\b)"
            r"|(?:\b(?:\d+(?:\.\d+)?\s*(?:inches?|in\\.|\"|feet|ft|'))\s+(?:wide|high)\s+doors?\b)",
            lower,
        ))
        rating = bool(re.search(
            r"(?:\b(?:fire[- ]?rated|rated|rating)\s+doors?\b)"
            r"|(?:\bdoors?\s+(?:rating|label|assembly|fire[- ]?rating)\b)"
            r"|(?:\b(?:doors?|openings?)\b[^.\n]{0,70}\b(?:fire[- ]?rating|rated\s+assembly|label)\b)",
            lower,
        ))
        return {"size": size, "rating": rating}

    def _local_relevance(self, subject: str, row: dict[str, Any]) -> dict[str, Any]:
        """Return a deterministic, auditable relevance decision for one row.

        This is intentionally conservative.  A semantic retriever may return
        a neighbouring discipline, but a neighbouring discipline is not
        evidence for the requested topic.  The excerpt is kept for the UI and
        for later audit; it is never generated from model output.
        """
        # Topic relevance must be supported by the evidence text itself.
        # Metadata such as discipline/category is useful for display and
        # filtering, but it cannot turn an unrelated comment into evidence.
        text = verified_text(row).strip()
        lower = text.casefold()
        topic = _controlled_topic(subject)
        if topic == "fire_separation":
            # A dwelling, opening, garage, property line, or generic assembly
            # is not by itself fire-separation evidence.  Require an explicit
            # fire/rating concept or one of the controlled compound phrases.
            strong = ("fire", "rated", "rating", "separation", "r302", "one-hour", "1-hour")
            phrases = tuple(phrase.replace("-", " ") for phrase in CONTROLLED_TOPIC_TERMS[topic] if " " in phrase)
            normalized = lower.replace("-", " ")
            phrase_hits = [phrase for phrase in phrases if phrase in normalized]
            token_hits = [token for token in strong if re.search(rf"\b{re.escape(token)}\b", lower)]
            # Words such as ``property`` or ``plan`` are not evidence of fire
            # separation by themselves.  Require a strong fire/rating concept.
            # Compound questions (for example, "setback and fire
            # separation") intentionally retain records for either explicit
            # concept; a narrow fire-separation comparison does not.
            compound_setback = "setback" in str(subject).casefold() and "fire separation" in str(subject).casefold().replace("-", " ")
            is_relevant = bool(phrase_hits or token_hits or (compound_setback and re.search(r"\bsetback\b", lower)))
            if is_relevant and any(word in lower for word in ("grading", "drainage", "stormwater", "runoff")) and not any(word in lower for word in ("fire", "rated", "rating", "separation", "r302")):
                is_relevant = False
            matched = phrase_hits[0] if phrase_hits else (token_hits[0] if token_hits else ("setback" if compound_setback and "setback" in lower else ""))
            confidence = 0.96 if phrase_hits or len(token_hits) >= 2 else (0.82 if token_hits else 0.0)
            reason = "Evidence contains a controlled fire-separation concept." if is_relevant else "Evidence does not concern fire separation."
        elif topic == "drainage":
            aliases = {
                "drainage", "drain", "runoff", "stormwater", "swale",
                "infiltration", "detention", "downspout", "discharge",
            }
            hits = [term for term in aliases if re.search(rf"\b{re.escape(term)}\b", lower)]
            is_relevant = bool(hits)
            matched = hits[0] if hits else ""
            confidence = 0.9 if len(hits) >= 2 else (0.8 if hits else 0.0)
            reason = "Evidence explicitly concerns drainage or water conveyance." if is_relevant else "Evidence may concern grading, but it does not explicitly concern drainage."
        elif topic == "tree_protection":
            normalized = lower.replace("-", " ")
            protection_patterns = (
                r"\btree protection\b", r"\bprotection (?:measure|zone|fenc|plan)",
                r"\b(?:fenc|mulch|plywood|trunk protection|soil compaction protection)\w*\b",
                r"\broot (?:prun|protection|impact|zone)",
                r"\b(?:tree|arborist)\b[^.\n]{0,100}\b(?:impact mitigation|monitoring inspection|protect(?:ion|ed|ing)?)\b",
                r"\b(?:heritage|protected) trees?\b[^.\n]{0,120}\b(?:impact|proximity|mitigat|protect)",
                r"\bproject arborist\b[^.\n]{0,100}\b(?:verify|monitor|inspection|root pruning|excavation)",
            )
            hits = [pattern for pattern in protection_patterns if re.search(pattern, normalized)]
            is_relevant = bool(hits)
            matched = "tree protection measures" if hits else ""
            confidence = 0.96 if len(hits) >= 2 else (0.88 if hits else 0.0)
            reason = (
                "Evidence explicitly concerns tree-protection measures or impact mitigation."
                if is_relevant else
                "Evidence concerns tree inventory, location, or removal, but not tree-protection measures."
            )
        elif topic == "tree_related":
            hits = re.findall(
                r"\b(?:tree|trees|arborist|arborist report|root|canopy|heritage|removal|preservation|inventory)\b",
                lower,
            )
            is_relevant = bool(hits)
            matched = "tree-related review requirement" if hits else ""
            confidence = 0.94 if len(set(hits)) >= 2 else (0.86 if hits else 0.0)
            reason = (
                "Evidence directly concerns a tree, arborist, removal, inventory, impact, or protection requirement."
                if is_relevant else
                "Evidence does not concern a tree-related review requirement."
            )
        elif topic in {"door_size", "door_rating", "door_attributes"}:
            signals = self._door_attribute_signals(lower)
            wanted = {"door_size": "size", "door_rating": "rating", "door_attributes": None}[topic]
            is_relevant = bool(signals["size"] if wanted == "size" else signals["rating"] if wanted == "rating" else (signals["size"] or signals["rating"]))
            matched = "door size/dimension" if signals["size"] and (wanted in {None, "size"}) else ("door rating/assembly" if signals["rating"] else "")
            confidence = 0.94 if (signals["size"] and signals["rating"] and wanted is None) else (0.9 if is_relevant else 0.0)
            label = {"door_size": "door size", "door_rating": "door rating", "door_attributes": "door size or rating"}[topic]
            reason = f"Evidence contains an explicit {label} requirement." if is_relevant else f"Evidence mentions doors but does not contain an explicit {label} requirement."
        else:
            is_relevant = self._candidate_matches_subject(subject, row)
            matched = next(iter(_topic_terms(subject) & _significant_terms(text)), "") if is_relevant else ""
            confidence = 0.8 if is_relevant else 0.0
            reason = "Evidence has an observable connection to the requested topic." if is_relevant else "Evidence has no observable connection to the requested topic."
        record_topic = str(row.get("discipline") or row.get("category") or "unknown")
        if topic == "tree_protection" and not is_relevant and re.search(r"\b(?:tree|trees|arborist|removal)\b", lower):
            record_topic = "tree inventory, location, or removal"
        elif topic == "drainage" and not is_relevant and re.search(r"\b(?:grading|slope)\b", lower):
            record_topic = "grading without explicit drainage evidence"
        if topic == "fire_separation" and any(word in lower for word in ("grading", "drainage", "stormwater", "runoff")) and not any(word in lower for word in ("fire", "rated", "rating", "separation", "r302")):
            record_topic = "grading and drainage permit"
        excerpt = _clean_text(text, 280)
        return {
            "is_relevant": bool(is_relevant),
            "matched_concept": matched,
            "supporting_excerpt": excerpt,
            "confidence": confidence,
            "exclude_reason": "" if is_relevant else reason,
            "record_topic": record_topic,
        }

    def _controlled_candidate_matches(self, subject: str, row: dict[str, Any]) -> bool:
        """High-recall prefilter; this never makes a record authoritative."""
        topic = _controlled_topic(subject)
        lower = verified_text(row).casefold().replace("-", " ")
        if topic in {"tree_protection", "tree_related"}:
            return bool(re.search(r"\b(?:tree|trees|arborist|root|canopy|preservation|removal)\b", lower))
        if topic == "drainage":
            return bool(re.search(r"\b(?:drainage|drain|runoff|stormwater|swale|infiltration|detention|downspout|grading|slope|discharge)\b", lower))
        if topic == "fire_separation":
            return bool(re.search(r"\b(?:fire|rated|rating|separation|assembly|gypsum|sprinkler|opening|eave|penetration|garage|dwelling|r302|ul)\b", lower))
        if topic in {"door_size", "door_rating", "door_attributes"}:
            return bool(re.search(r"\bdoors?\b", lower))
        return self._candidate_matches_subject(subject, row)

    def _validate_retrieved_rows(
        self,
        subject: str,
        rows: list[dict[str, Any]],
        *,
        local_fallback_intent: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str]:
        """Apply the hard topic gate, then an optional bounded Gemini check."""
        kept: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        warnings: list[str] = []
        local_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            decision = self._local_relevance(subject, row)
            local_by_id[str(row.get("comment_id", ""))] = decision
            # Local rules are a high-recall audit hint, not the final
            # authority. Send every candidate onward so unfamiliar but valid
            # wording is not discarded before semantic validation.
            kept.append(row)

        # Evidence verification is accuracy-critical.  It deliberately does
        # not use the prose-generation circuit breaker: a previous summary
        # timeout must never cause the next question to trust local candidates
        # or silently skip semantic validation.
        # Relevance is the authority boundary, but this is still part of the
        # conversational request.  Use the dedicated Knowledge Chat client
        # first (normally Gemini Flash-Lite), then fall back to the Smart
        # Search client only when the dedicated client is not configured.
        # Using the document/search client here made ordinary questions wait
        # on its slower model and caused valid candidates to be reported as
        # "Gemini evidence verification unavailable" when that model timed
        # out or hit its separate capacity circuit.
        configured_client = self.store.knowledge_gemini_client or self.store.gemini_client
        client = configured_client
        remote: dict[str, dict[str, Any]] = {}
        candidates = [{
            "candidate_id": str(row.get("comment_id", "")),
            "city": row.get("city", ""),
            "project": _project_label(row),
            "discipline": row.get("discipline", ""),
            "comment_text": verified_text(row),
            "response_text": verified_text(self._confirmed_response(row.get("comment_id", ""))) if self._confirmed_response(row.get("comment_id", "")) else "",
        } for row in kept]
        verification_complete = not candidates
        remote_failed = False
        try:
            if client and candidates:
                for batch in self._verification_batches(candidates):
                    decisions = self._verify_candidate_batch(client, subject, batch)
                    for item in decisions:
                        if isinstance(item, dict) and str(item.get("candidate_id", "")):
                            remote[str(item["candidate_id"])] = item
                verification_complete = {item["candidate_id"] for item in candidates} <= set(remote)
                remote_failed = not verification_complete
            elif candidates:
                # The validated tag index and deterministic hard relevance
                # gate are sufficient for a bounded local answer when the
                # question only asks for counts/searchable records. For a
                # handling summary, require an already-confirmed response;
                # comment-only candidates remain quarantined until semantic
                # verification is available. This keeps the no-Gemini test
                # path deterministic without weakening the evidence boundary.
                # Controlled topics require the dedicated relevance validator
                # for handling/comparison claims.  A lexical/tag match alone
                # is not enough to certify that the record actually answers
                # the requested topic (the grading-vs-fire failure mode).
                # Uncontrolled legacy lookups may still use the deterministic
                # local gate when a confirmed response is already present.
                if local_fallback_intent == "historical_response_summary" and _controlled_topic(subject):
                    remote_failed = True
                    verification_complete = False
                    locally_verified = []
                else:
                    locally_verified = []
                for row in kept:
                    decision = local_by_id[str(row.get("comment_id", ""))]
                    if not decision.get("is_relevant"):
                        continue
                    if local_fallback_intent == "historical_response_summary" and not self._confirmed_response(row.get("comment_id", "")):
                        continue
                    locally_verified.append(row)
                if len(locally_verified) == len(kept):
                    remote = {
                        str(row.get("comment_id", "")): {
                            "candidate_id": str(row.get("comment_id", "")),
                            "is_relevant": True,
                            "confidence": float(local_by_id[str(row.get("comment_id", ""))].get("confidence", 0.8) or 0.8),
                        }
                        for row in locally_verified
                    }
                    verification_complete = True
                else:
                    remote_failed = True
        except (RuntimeError, ValueError, TypeError) as exc:
            self._mark_remote_failure()
            remote_failed = True
            verification_complete = False
            failure_kind = self._verification_failure_kind(exc)
            if failure_kind == "capacity":
                warnings.append("Gemini evidence verification could not run because API credits or rate capacity are unavailable. Retrieved candidates were not treated as evidence.")
            elif failure_kind == "network":
                warnings.append("Gemini evidence verification timed out or could not reach the service. Retrieved candidates were not treated as evidence; retrying the question is safe.")
            elif failure_kind == "model":
                warnings.append("The configured Gemini evidence-verification model is unavailable. Retrieved candidates were not treated as evidence.")
            elif failure_kind == "incomplete":
                warnings.append("Gemini returned incomplete structured evidence verification after smaller-packet retries. Retrieved candidates were not treated as evidence.")
            else:
                warnings.append("Gemini evidence verification was unavailable. Retrieved candidates are not presented as source-grounded answers.")

        if remote_failed:
            warnings.append("Evidence validation was incomplete; no authoritative handling or comparison conclusion was generated.")
        if remote and verification_complete:
            validated: list[dict[str, Any]] = []
            for row in kept:
                item = remote.get(str(row.get("comment_id", "")))
                if item and bool(item.get("is_relevant")) and float(item.get("confidence", 0) or 0) >= 0.75:
                    validated.append(row)
                else:
                    decision = local_by_id[str(row.get("comment_id", ""))]
                    excluded.append({
                        "comment_id": row.get("comment_id", ""),
                        "project": _project_label(row),
                        "city": row.get("city", ""),
                        "record_topic": decision["record_topic"],
                        "exclude_reason": str((item or {}).get("exclude_reason") or "Gemini relevance confidence was below the validation threshold."),
                        "supporting_excerpt": str((item or {}).get("supporting_excerpt") or decision["supporting_excerpt"]),
                    })
            kept = validated
        status = "validated" if verification_complete else "unverified"
        return kept, excluded, warnings, status

    def _local_subject(self, message: str, proposed: str) -> str:
        """Keep a router paraphrase only when it still reflects the question."""
        local = _significant_terms(message)
        proposed_terms = _significant_terms(proposed)
        if not local or not proposed_terms:
            return proposed or "the requested topic"
        # Do not let the model narrow a broad "tree-related" question into
        # the materially different "tree protection" subset. The user's own
        # wording controls scope; protection/impact/root language must be
        # present in the question before the narrow topic is selected.
        if "tree" in local and not (local & {"protect", "protection", "impact", "root", "preservation"}):
            return "tree"
        # A paraphrase can use an alias (tree -> arborist, drainage -> runoff),
        # but an entirely disjoint subject is almost always a routing error.
        if _topic_terms(message) & _topic_terms(proposed):
            # Normalize boilerplate such as ``setback-related`` and
            # ``from drainage`` so retrieval sees the topic rather than the
            # grammatical wrapper around it.
            # Preserve a compound request before checking single-word topics.
            # Otherwise a router phrase such as “setback and fire separation”
            # is reduced to just “setback”, which silently drops the second
            # side of direct/related comparison tests.
            if {"setback", "fire", "separation"} <= (local | proposed_terms):
                return "setback fire separation"
            canonical_pairs = (
                ({"tree", "protection"}, "tree protection"),
                ({"drainage"}, "drainage"),
                ({"door", "size"}, "door size"),
                ({"door", "rating"}, "door rating"),
                ({"fire", "separation"}, "fire separation"),
                ({"setback"}, "setback"),
                ({"exterior", "wall", "rating"}, "exterior wall ratings"),
            )
            for required, label in canonical_pairs:
                if required <= local or required <= proposed_terms:
                    return label
            return " ".join(sorted(proposed_terms))
        return " ".join(sorted(local))

    def _literal_fallback(self, subject: str, filters: dict[str, str], unavailable: bool, allow_unverified: bool = False) -> tuple[list[str], dict[str, str], list[str]]:
        fallback = self._keyword_ids(subject, filters)
        # A historical-handling answer must never silently downgrade to raw
        # lexical matches when there is no dedicated chat verifier at all.
        # The caller may still opt into bounded unverified candidates when a
        # chat client is configured (and can verify them independently).
        if unavailable and self.store.knowledge_gemini_client is None and not allow_unverified:
            # Keep only records that already have a confirmed response.  This
            # preserves useful, auditable history while ensuring a comment-only
            # lexical hit cannot be presented as an answer about handling.
            fallback = [item for item in fallback if self._confirmed_response(item)]
            if not fallback:
                return [], {}, [
                    "Semantic verification was unavailable. No unverified literal matches were used as evidence."
                ]
            return fallback, {item: "unverified" for item in fallback}, [
                "Semantic verification was unavailable. Only literal matches with an existing confirmed response are shown as unverified evidence."
            ]
        verified_ids, verified_classes = self._chat_verify_literal_ids(subject, fallback)
        if verified_ids:
            reason = "Smart Search verification was unavailable" if unavailable else "Smart Search returned no verified precedent"
            return verified_ids, verified_classes, [
                f"{reason}; the dedicated Gemini Knowledge Chat model independently verified the bounded literal candidates used by this chat answer."
            ]
        return fallback, {item: "unverified" for item in fallback}, [
            "Semantic verification was unavailable. Literal database matches are shown as unverified evidence and are not used to claim historical company handling."
        ]

    def _smart_ids(
        self,
        subject: str,
        filters: dict[str, str],
        *,
        intent: str = "precedent_search",
        allow_unverified: bool = False,
        force_stage3: bool = False,
    ) -> tuple[list[str], dict[str, str], list[str]]:
        # Knowledge Chat is deliberately local-first.  The full Smart Search
        # pipeline performs query analysis, rewrites, embeddings, candidate
        # evaluation, deep reranking, and verification; using it inside Chat
        # multiplied one question into dozens of serial Gemini requests.
        # Build a high-recall local pool here, then let the single evidence gate
        # below perform at most one bounded Gemini validation call.
        controlled = _controlled_topic(subject)
        # Progressive retrieval is the authoritative candidate selector for
        # controlled topics.  It keeps the tag index as a rebuildable search
        # projection and applies the hard topic gate before the normal Chat
        # evidence validator, so source occurrences and unrelated neighbours
        # cannot inflate the candidate pool.
        # A compound comparison must retain both requested concepts. A single
        # controlled-topic index cannot represent "setback and fire
        # separation" without dropping one side, so use the existing broad
        # candidate path for that explicit compound query.
        compound_setback_fire = (
            "setback" in subject.casefold()
            and "fire separation" in subject.casefold().replace("-", " ")
        )
        # A user-forced broader search must inspect the whole verified city
        # corpus even when the query has no taxonomy label yet.
        use_progressive = force_stage3 or (bool(controlled) and not compound_setback_fire)
        if use_progressive:
            overlay = getattr(self.store, "_tag_overlaid_rows", None)
            retrieval_rows = overlay() if callable(overlay) else self.store._comments
            progressive = progressive_retrieve(
                subject,
                retrieval_rows,
                intent=intent,
                filters=filters,
                tag_index=self._progressive_index(),
                force_stage3=force_stage3,
            )
            self._last_progressive_result = progressive.as_dict()
            ids = [
                str(row.get("comment_id", ""))
                for row in progressive.rows
                if str(row.get("comment_id", "")) in self.store._comments_by_id
            ]
            ids = list(dict.fromkeys(ids))
            # A question about how the company handled an issue can only be
            # supported by events with a confirmed response. Comment-only
            # rows remain represented in progressive coverage/limitations,
            # but sending them to Gemini makes the validation packet larger
            # without adding any handling evidence.
            if intent == "historical_response_summary":
                ids = [item for item in ids if self._confirmed_response(item)]
            ids = ids[:80]
            if ids:
                classes = {
                    item: ("direct" if int(self._last_progressive_result.get("retrieval_stage_used", 3)) == 1 else "related")
                    for item in ids
                }
                return ids, classes, [
                    f"Progressive retrieval stage {self._last_progressive_result.get('retrieval_stage_used', 3)} selected canonical candidates for topic validation."
                ]
            if intent == "historical_response_summary" and progressive.rows:
                return [], {}, [
                    "Relevant canonical comments were found, but none has a confirmed company response that can support a handling summary.",
                    "Evidence validation was incomplete; no authoritative handling or comparison conclusion was generated.",
                ]
            if self._last_progressive_result.get("excluded"):
                return [], {}, ["No validated canonical events matched the controlled topic; off-topic candidates were excluded."]
        ids: list[str] = []
        if controlled and use_progressive:
            ids = [
                row["comment_id"]
                for row in self.store._comments
                if self._record_matches_filters(row, filters)
                and self._controlled_candidate_matches(subject, row)
            ]
        else:
            ids.extend(self._keyword_ids(subject, filters))
            try:
                local_results = self.store.search(filters.get("city", ""), subject, 60)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                local_results = []
            ids.extend(
                str(item.get("comment_id", ""))
                for item in local_results
                if str(item.get("comment_id", "")) in self.store._comments_by_id
            )

        # Preserve the legacy hybrid classifier only for an explicit
        # multi-topic comparison. This is not the normal progressive path;
        # it prevents a compound query from losing the second concept while
        # keeping direct/related classes supplied by the existing verifier.
        if compound_setback_fire and self.store.gemini_client and hasattr(self.store, "gemini_search"):
            try:
                hybrid = self.store.gemini_search(
                    filters.get("city", ""),
                    subject,
                    10,
                    filters.get("discipline", ""),
                    filters.get("category", ""),
                )
                hybrid_results = hybrid.get("results", []) if isinstance(hybrid, dict) else []
                verified_ids = [
                    str(item.get("comment_id", ""))
                    for item in hybrid_results
                    if str(item.get("comment_id", "")) in self.store._comments_by_id
                    and item.get("match_class") in {"direct", "related"}
                ]
                if verified_ids:
                    ids = list(dict.fromkeys(verified_ids))
                    classes = {
                        str(item.get("comment_id")): str(item.get("match_class"))
                        for item in hybrid_results
                        if str(item.get("comment_id", "")) in ids
                        and item.get("match_class") in {"direct", "related"}
                    }
                    return ids[:80], classes, ["Compound comparison used the existing bounded hybrid classifier for direct/related grouping."]
            except (RuntimeError, TypeError, ValueError):
                # The deterministic broad pool below remains available and
                # will still pass through the normal evidence gate.
                pass

        ids = list(dict.fromkeys(
            item for item in ids
            if item in self.store._comments_by_id
            and self._record_matches_filters(self.store._comments_by_id[item], filters)
            and (
                self._controlled_candidate_matches(subject, self.store._comments_by_id[item])
                if controlled and use_progressive else self._local_relevance(subject, self.store._comments_by_id[item])["is_relevant"]
            )
        ))[:80]
        classes = {item: "direct" for item in ids}
        if not ids:
            return [], {}, ["No locally relevant records passed the evidence gate for this topic."]
        return ids, classes, ["Local retrieval produced a candidate pool; only records that pass complete Gemini topic validation may support the answer."]

    def _metrics(self, comment_ids: list[str]) -> dict[str, int]:
        unique_ids = list(dict.fromkeys(comment_ids))
        rows = [self.store._comments_by_id[item] for item in unique_ids]
        confirmed = sum(self._confirmed_response(item) is not None for item in unique_ids)
        searchable_units = sum(
            len(self.store.search_index.records.get(item, {}).get("search_units", [])) or 1
            for item in unique_ids
        )
        return {
            "parent_comments": len(unique_ids),
            "searchable_units": searchable_units,
            "projects": len({_project_key(row) for row in rows}),
            "review_rounds": len({(_project_key(row), row.get("review_round", "")) for row in rows}),
            "confirmed_responses": confirmed,
            "missing_responses": sum(not row.get("response_id") for row in rows),
            "unconfirmed_responses": len(rows) - confirmed - sum(not row.get("response_id") for row in rows),
            "canonical_issues": len({row.get("canonical_issue_id") for row in rows if row.get("canonical_issue_id")}),
        }

    def _citations(self, comment_ids: list[str], limit: int = 5) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        for comment_id in comment_ids[:limit]:
            view = self.store._view_comment(self.store._comments_by_id[comment_id])
            for role, owner in (("comment", view), ("response", view.get("response") or {})):
                if role == "response" and not self._confirmed_response(comment_id):
                    continue
                source = next((item for item in owner.get("sources", []) if item.get("kind") == "local"), None)
                if source:
                    citations.append({
                        "comment_id": comment_id, "role": role, "source_id": source["source_id"],
                        "label": f"{role.title()} source · {source.get('filename', '')}",
                    })
        return citations

    @staticmethod
    def _evidence_level(row: dict[str, Any], response: dict[str, Any] | None) -> tuple[int, str]:
        """Classify the strength of evidence for an applicant-handling claim."""
        if not response:
            return 1, "Comment only; no stored applicant response"
        response_text = verified_text(response).casefold()
        concrete_revision = bool(re.search(
            r"\b(?:sheet|page|detail|plan|drawing|calculation|revision|revised|updated|added|replaced|removed|installed|provided|dimension|elevation|schedule)\b",
            response_text,
        ))
        # Older records store timeline events directly on the comment, while
        # normalized imports keep them under issue_thread.events.  Read both
        # shapes so a later reviewer confirmation is never downgraded merely
        # because the record came from a different ingestion generation.
        events = row.get("issue_thread_events") or []
        issue_thread = row.get("issue_thread")
        if not events and isinstance(issue_thread, dict):
            events = issue_thread.get("events") or []
        def _is_later_confirmation(event: dict[str, Any]) -> bool:
            event_type = str(event.get("event_type", "")).casefold()
            if event_type == "reviewer_follow_up":
                return True
            if event_type != "government_comment":
                return False
            # A later government row can confirm closure, but the original
            # comment itself must not be treated as a confirmation merely
            # because it mentions an approved assembly or completed plan.
            try:
                original_round = int(str(row.get("review_round", "")).strip())
                event_round = int(str(event.get("effective_round") or event.get("review_round") or "").strip())
            except (TypeError, ValueError):
                return False
            return event_round > original_round

        reviewer_confirmation = any(
            _is_later_confirmation(event)
            and bool(re.search(
                r"\b(?:resolved|complete|completed|approved|addressed|satisfied|no further comments|closed)\b",
                str(event.get("exact_text") or event.get("text") or "").casefold(),
            ))
            for event in events if isinstance(event, dict)
        )
        if reviewer_confirmation:
            return 4, "Later reviewer record confirms or closes the issue"
        if concrete_revision:
            return 3, "Applicant response names a concrete revision or source location"
        return 2, "Applicant response is recorded, but without a concrete revision detail"

    def _answer(self, message: str, plan: dict[str, Any], metrics: dict[str, int], rows: list[dict[str, Any]], breakdowns: dict[str, dict[str, int]]) -> dict[str, str]:
        subject = plan["subject"] or "the requested topic"
        candidate_metrics = plan.get("_candidate_metrics") or metrics
        if plan.get("_validation_status") == "unverified" and plan.get("evidence_scope") != "literal_unverified":
            database = (
                f"Unverified candidate set: the search returned {candidate_metrics['parent_comments']} possible comments across "
                f"{candidate_metrics['projects']} projects for {subject}. Semantic validation did not complete, so 0 comments are treated as authoritative evidence."
            )
        elif plan.get("_validation_status") in {"insufficient_comparison", "no_validated_evidence"} and plan.get("evidence_scope") != "literal_unverified":
            database = (
                f"Validated evidence result: {metrics['parent_comments']} relevant parent comments across "
                f"{metrics['projects']} projects remain for {subject}. "
                f"The initial search screened {candidate_metrics['parent_comments']} possible comments across "
                f"{candidate_metrics['projects']} projects."
            )
        elif plan.get("evidence_scope") == "literal_unverified":
            database = (
                f"Candidate result: {metrics['parent_comments']} original parent comments across "
                f"{metrics['projects']} projects literally match the topic terms for {subject}; semantic validation is still pending."
            )
        elif "load_filtered_comments" in plan["operations"]:
            database = (
                f"Database scope: {metrics['parent_comments']} original parent comments across "
                f"{metrics['projects']} projects and {metrics['review_rounds']} review rounds are included."
            )
        elif plan["intent"] == "aggregate_count":
            database = (
                f"Database result: {metrics['parent_comments']} original parent comments across "
                f"{metrics['projects']} projects and {metrics['review_rounds']} review rounds match the validated query for {subject}."
            )
        else:
            database = (
                f"Verified evidence set: {metrics['parent_comments']} original parent comments across "
                f"{metrics['projects']} projects and {metrics['review_rounds']} review rounds support this answer about {subject}."
            )
        confirmed_rows = [row for row in rows if self._confirmed_response(row["comment_id"])]
        disciplines = sorted({str(row.get("discipline", "unknown")) for row in rows})
        pattern = (
            f"Historical evidence: {metrics['confirmed_responses']} matching comments have confirmed historical responses. "
            f"The records span {', '.join(disciplines[:4]) or 'no recorded disciplines'}."
        )
        if confirmed_rows:
            # Always prepare a useful deterministic answer before the optional
            # Gemini prose pass.  If the model is slow or unavailable, users
            # still see the actual recorded handling rather than only a count.
            examples: list[str] = []
            seen_examples: set[str] = set()
            for row in confirmed_rows:
                response = self._confirmed_response(row["comment_id"])
                response_text = _clean_text(verified_text(response) if response else "", 260)
                normalized = response_text.casefold()
                if not response_text or normalized in seen_examples:
                    continue
                seen_examples.add(normalized)
                examples.append(f"**{_project_label(row)}:** {response_text}")
                if len(examples) == 3:
                    break
            if examples:
                pattern = (
                    f"The history shows {metrics['confirmed_responses']} confirmed responses to {subject} comments. "
                    "Recorded approaches included:\n\n- " + "\n- ".join(examples)
                )
        client = self._chat_client()
        if "load_filtered_comments" in plan["operations"]:
            lower = message.casefold()
            requested_key = next((key for needle, key in (
                ("discipline", "disciplines"), ("project", "projects"), ("round", "review_rounds"),
                ("reviewer", "reviewers"), ("code", "code_sections"), ("response", "response_status"),
                ("city", "cities"),
            ) if needle in lower), "disciplines")
            selected = breakdowns.get(requested_key, {})
            detail = "; ".join(f"{name}: {count}" for name, count in list(selected.items())[:8]) or "none recorded"
            _, topics = self.store._common_topics(rows, limit=5)
            topic_rows = [{
                "representative_text": _clean_text(item["label"], 180),
                "parent_comments": int(item["occurrences"]),
                "projects": int(item["projects"]),
            } for item in topics]
            topic_text = "; ".join(f"{item['representative_text']} ({item['parent_comments']})" for item in topic_rows) or "no repeated topic group was detected"
            pattern = f"Database breakdown — {requested_key.replace('_', ' ')}: {detail}. Common recurring topics: {topic_text}."
            if client and hasattr(client, "summarize_database_scope"):
                facts = {
                    "exact_metrics": metrics,
                    "database_breakdowns": breakdowns,
                    "representative_recurring_topics": topic_rows,
                }
                try:
                    summary = _clean_text(client.summarize_database_scope(message, facts), 2400)
                    if summary:
                        pattern = f"Historical summary: {summary}"
                except RuntimeError:
                    self._mark_remote_failure()
                    pass
        elif plan.get("evidence_scope") == "literal_unverified" and rows:
            # Do not run the quadratic common-topic similarity pass on an
            # unverified fallback pool.  It is both unnecessary for a
            # non-authoritative answer and can turn a broad comparison into a
            # multi-minute request.  Counts and disciplines are enough to
            # explain the screened candidate set; canonical-topic analysis is
            # reserved for validated result sets.
            pattern = (
                f"Comment evidence: these records concern {subject}, but semantic precedent verification did not complete. "
                f"{metrics['confirmed_responses']} have confirmed responses across "
                f"{metrics['projects']} projects; no company-handling conclusion is drawn from unverified matches."
            )
            if client and hasattr(client, "summarize_database_scope"):
                try:
                    summary = _clean_text(client.summarize_database_scope(message, {
                        "exact_metrics": metrics,
                        "database_breakdowns": breakdowns,
                        "representative_recurring_topics": [],
                        "evidence_status": "literal matches; semantic precedent unverified",
                    }), 1800)
                    if summary:
                        pattern = f"Comment evidence: {summary} No company-handling conclusion is drawn from unverified matches."
                except RuntimeError:
                    self._mark_remote_failure()
                    pass
        if (
            confirmed_rows
            and plan.get("_validation_status", "not_required") in {"validated", "not_required"}
            and plan.get("evidence_scope") != "literal_unverified"
            and "load_filtered_comments" not in plan["operations"]
            and "summarize_confirmed_responses" in plan["operations"]
            and client and hasattr(client, "summarize_knowledge_evidence")
            and not hasattr(client, "synthesize_knowledge_answer")
        ):
            evidence = []
            for row in confirmed_rows[:30]:
                response = self._confirmed_response(row["comment_id"])
                evidence.append({
                    "project": _project_label(row), "city": row.get("city", ""),
                    "review_round": row.get("review_round", ""), "discipline": row.get("discipline", ""),
                    "comment_text": verified_text(row),
                    "confirmed_response_text": verified_text(response) if response else "",
                })
            try:
                summary = _clean_text(client.summarize_knowledge_evidence(plan["subject"], evidence), 2000)
                if summary:
                    pattern = f"Historical pattern: {summary}"
            except RuntimeError:
                self._mark_remote_failure()
                pass
        if plan["intent"] == "compare_groups":
            subject_groups = plan.get("comparison_subject_groups") or []
            if subject_groups:
                group_counts: dict[str, int] = {}
                for group in subject_groups:
                    group_terms = _significant_terms(group)
                    group_counts[group] = sum(1 for row in rows if group_terms & _significant_terms(verified_text(row)))
                pattern = "Topic comparison: " + "; ".join(f"{name}: {count} comments" for name, count in group_counts.items()) + "."
            else:
                groups: dict[str, int] = {}
                for row in rows:
                    project = _project_label(row)
                    groups[project] = groups.get(project, 0) + 1
                project_coverage = "; ".join(
                    f"{name}: {count} comment{'s' if count != 1 else ''}"
                    for name, count in sorted(groups.items(), key=lambda item: (-item[1], item[0].casefold()))[:6]
                )
                if pattern.startswith("Historical pattern:") or pattern.startswith("The history shows"):
                    pattern = f"{pattern}\n\nProject coverage: {project_coverage}."
                else:
                    pattern = "Project comparison: " + project_coverage + "."
        elif plan["intent"] == "aggregate_count":
            lower_message = message.casefold()
            if re.search(r"\bwhich\s+review\s+round\b", lower_message):
                grouped = breakdowns.get("review_rounds", {})
                if grouped:
                    name, count = max(grouped.items(), key=lambda item: (item[1], item[0]))
                    pattern = f"The most drainage comments appear in review round {name} ({count} comments)."
            elif re.search(r"\bwhich\s+city\b", lower_message):
                grouped = breakdowns.get("cities", {})
                if grouped:
                    name, count = max(grouped.items(), key=lambda item: (item[1], item[0]))
                    pattern = f"The city with the most matching comments is {name} ({count} comments)."
        elif plan["intent"] == "explain_selected_comment" and rows:
            selected = rows[0]
            response = self._confirmed_response(selected["comment_id"])
            pattern = f"Direct historical quotation: “{_clean_text(selected.get('original_text'), 1200)}”"
            if response:
                pattern += f" Confirmed historical response: “{_clean_text(response.get('original_text'), 1200)}”"
        limitation = (
            f"Data limitation: {metrics['missing_responses']} {('comment has' if metrics['missing_responses'] == 1 else 'comments have')} no stored response and "
            f"{metrics['unconfirmed_responses']} {('comment has' if metrics['unconfirmed_responses'] == 1 else 'comments have')} a response link that is not confirmed. Those links were not used to describe company actions."
        )
        if plan.get("_validation_status") == "unverified":
            limitation = (
                "Data limitation: response and resolution counts are withheld because the retrieved candidates have not passed semantic topic validation."
            )
            if plan.get("intent") == "compare_groups":
                pattern = (
                    "Historical evidence: No validated supporting precedent is available for this comparison. "
                    f"The database returned {candidate_metrics['parent_comments']} literal candidate comments across "
                    f"{candidate_metrics['projects']} projects, but they remain unverified."
                )
            else:
                pattern = (
                    "Historical evidence: Literal topic matches were found, but semantic verification did not complete. "
                    f"The database returned {candidate_metrics['parent_comments']} candidate comments across "
                    f"{candidate_metrics['projects']} projects; no company-handling conclusion is drawn from unverified matches."
                )
        elif plan.get("_validation_status") in {"insufficient_comparison", "no_validated_evidence"} and plan.get("evidence_scope") != "literal_unverified":
            pattern = (
                "Historical evidence: No validated supporting precedent is available for this question. "
                f"The database returned {candidate_metrics['parent_comments']} literal candidate comments across "
                f"{candidate_metrics['projects']} projects, but they were not used to make a factual comparison."
            )
        elif not rows:
            pattern = "Historical evidence: No verified supporting precedent is available for this question."
        elif not confirmed_rows and plan.get("evidence_scope") != "literal_unverified":
            pattern = "Historical evidence: Matching comments were found, but none has a confirmed response that can support a statement about company actions."
        return {"database_result": database, "historical_pattern": pattern, "data_limitation": limitation}

    @staticmethod
    def _without_label(value: Any) -> str:
        return re.sub(r"^[^:]{2,40}:\s*", "", str(value or "")).strip()

    def _natural_answer(
        self,
        message: str,
        plan: dict[str, Any],
        metrics: dict[str, int],
        rows: list[dict[str, Any]],
        sections: dict[str, str],
    ) -> str:
        """Create the conversational layer shown first in the chat.

        The older section strings remain in the API for auditability and
        backwards compatibility, but they are no longer the user-facing
        answer.  This function deliberately leads with the requested result
        and adds only the context needed to interpret it.
        """
        count = metrics["parent_comments"]
        projects = metrics["projects"]
        rounds = metrics["review_rounds"]
        subject = plan.get("subject") or "the requested topic"
        noun = "comment" if count == 1 else "comments"
        project_noun = "project" if projects == 1 else "projects"
        round_noun = "review round" if rounds == 1 else "review rounds"
        pattern = self._without_label(sections.get("historical_pattern", ""))
        limitation = self._without_label(sections.get("data_limitation", ""))
        intent = plan.get("intent")
        validation_status = plan.get("_validation_status")
        excluded = plan.get("_excluded_records") or []

        if intent == "compare_groups" and validation_status in {"insufficient_comparison", "no_validated_evidence", "unverified"}:
            topic = "fire separation" if _controlled_topic(subject) == "fire_separation" else subject
            if validation_status == "unverified":
                candidate_metrics = plan.get("_candidate_metrics") or metrics
                return (
                    f"I found **{candidate_metrics.get('parent_comments', 0)} literal candidate comments** across "
                    f"**{candidate_metrics.get('projects', 0)} projects** for **{topic}**, but they are not validated yet. "
                    "I cannot make a reliable cross-project comparison until semantic evidence verification completes."
                )
            if count == 0:
                candidate_metrics = plan.get("_candidate_metrics") or {}
                candidate_count = int(candidate_metrics.get("parent_comments", 0) or 0)
                candidate_projects = int(candidate_metrics.get("projects", 0) or 0)
                if candidate_count:
                    answer = (
                        f"I found **{candidate_count} literal candidate comments** across **{candidate_projects} projects** for **{topic}**, "
                        "but no validated evidence is available yet, so I cannot make a reliable cross-project comparison."
                    )
                else:
                    answer = f"I could not find enough relevant historical evidence to compare **{topic}** comments across projects."
                if excluded:
                    first = excluded[0]
                    project = first.get("project") or "the retrieved project"
                    record_topic = first.get("record_topic") or "another topic"
                    answer += f"\n\nThe current search returned {len(excluded)} record{'s' if len(excluded) != 1 else ''} from **{project}**, but {('those records concern' if len(excluded) != 1 else 'that record concerns')} **{record_topic}**—not {topic}—so I excluded {('them' if len(excluded) != 1 else 'it')} from the answer."
                else:
                    answer += "\n\nI did not find a relevant record in the validated results."
                if _controlled_topic(subject) == "fire_separation":
                    answer += "\n\nTry broadening the search to related terms such as **fire-rated wall, property-line wall, opening protection, exterior wall rating, eave projection, garage separation,** or **dwelling-unit separation**."
                return answer
            return f"I found only **{projects} relevant project** for **{topic}**, so I can summarize that record but cannot make a meaningful cross-project comparison."

        if validation_status == "unverified":
            candidate_metrics = plan.get("_candidate_metrics") or metrics
            candidate_count = int(candidate_metrics.get("parent_comments", 0) or 0)
            candidate_projects = int(candidate_metrics.get("projects", 0) or 0)
            return (
                f"I found **{candidate_count} literal candidate comments** across **{candidate_projects} projects** for **{subject}**, "
                "but they are not semantically validated yet. I cannot summarize confirmed handling or resolutions until evidence verification completes."
            )

        if not count:
            answer = f"I couldn’t find a verified historical comment directly related to **{subject}** in the selected scope."
            if excluded:
                topics = list(dict.fromkeys(str(item.get("record_topic") or "an adjacent topic") for item in excluded))[:3]
                answer += (
                    f" I excluded **{len(excluded)} adjacent candidate{'s' if len(excluded) != 1 else ''}** "
                    f"because they concerned {', '.join(topics)} rather than directly answering the question."
                )
            return answer

        if intent == "aggregate_count":
            if re.search(r"\bwhich\s+(?:review\s+round|city)\b", message.casefold()) and pattern and not pattern.lower().startswith("historical evidence"):
                return pattern
            if re.search(r"\bhow\s+many\s+projects?\b", message.casefold()):
                return f"I found **{projects} projects** with historical comments related to **{subject}**."
            answer = f"I found **{count} historical {noun}** related to **{subject}**, covering **{projects} {project_noun}** and **{rounds} {round_noun}**."
            if metrics["missing_responses"]:
                missing = metrics["missing_responses"]
                answer += f" {missing} {('comment has' if missing == 1 else 'comments have')} no confirmed applicant response in the database."
            return answer

        if intent == "compare_groups":
            answer = pattern or f"The selected groups contain {count} historical {noun}."
            return f"I found **{count} historical {noun}** across **{projects} {project_noun}**. {answer}"

        if intent == "explain_selected_comment":
            return pattern or f"This record is one historical {noun} in the selected project."

        if intent in {"historical_response_summary", "topic_summary", "database_exploration"} and pattern:
            # Coverage and limitations are rendered as supporting sections;
            # keep the first message focused on the actual conclusion.
            return pattern

        answer = f"I found **{count} historical {noun}** related to **{subject}**, covering **{projects} {project_noun}** and **{rounds} {round_noun}**."
        if pattern and not pattern.lower().startswith("no verified"):
            answer += f"\n\n{pattern}"
        if limitation and (metrics["missing_responses"] or metrics["unconfirmed_responses"]):
            answer += f"\n\n{limitation}"
        return answer

    @staticmethod
    def _presentation_type(message: str, plan: dict[str, Any]) -> str:
        """Map retrieval intent to the answer shape the UI should render."""
        lower = str(message or "").casefold()
        if re.search(r"\b(?:what happened|timeline|history of (?:the|this)|from pc\d|across rounds?)\b", lower):
            return "TIMELINE"
        if re.search(r"\b(?:what should (?:we|i) learn|lessons?|before submission|safer approach|what should (?:we|i) check)\b", lower):
            return "PRACTICAL_LESSONS"
        intent = str(plan.get("intent", ""))
        if intent == "aggregate_count":
            return "COUNT"
        if intent == "historical_response_summary":
            return "HOW_HANDLED"
        if intent in {"topic_summary", "database_exploration"}:
            return "HISTORY_SUMMARY"
        if intent == "compare_groups":
            return "COMPARISON"
        if intent == "precedent_search":
            return "EXAMPLE_SEARCH" if re.search(r"\b(?:examples?|precedents?|show\s+(?:me\s+)?\d+)\b", lower) else "FACT_LOOKUP"
        if intent == "filter_previous_results":
            return "FOLLOW_UP"
        return "FACT_LOOKUP"

    @staticmethod
    def _support_level(event_ids: list[str], project_ids: list[str]) -> str:
        if len(set(project_ids)) > 1:
            return "cross_project"
        if len(set(event_ids)) > 1 and project_ids:
            return "single_project"
        if len(set(event_ids)) > 1:
            return "multiple_records"
        return "single_record"

    @staticmethod
    def _response_pattern(response_text: str) -> tuple[str, str, str]:
        """Classify a recorded action without asking a model to invent one."""
        lower = response_text.casefold()
        if re.search(r"\b(?:sheet|detail|page|drawing|plan set|calculation|report|schedule)\b", lower):
            return (
                "Specific drawing or document revision",
                "The response tied the proposed correction to a named plan, sheet, detail, calculation, report, or schedule.",
                "The recorded action gave the reviewer a concrete place to verify the revision.",
            )
        if re.search(r"\b(?:permit|application|submitt(?:ed|al)|upload(?:ed)?)\b", lower):
            return (
                "Separate supporting submittal",
                "The response addressed the comment through a separate permit, application, report, or uploaded supporting document.",
                "The recorded action identified an additional submittal rather than only acknowledging the request.",
            )
        if re.search(r"\b(?:removed|deleted|relocated|redesigned|reconfigured|replaced)\b", lower):
            return (
                "Design changed to remove the conflict",
                "The applicant described changing, removing, relocating, or replacing the affected design condition.",
                "The recorded action changed the design itself.",
            )
        if re.search(r"\b(?:added|provided|installed|included|shown|labeled|labelled|identified)\b", lower):
            return (
                "Requested information or feature added",
                "The response stated that requested information, labeling, details, or a physical feature had been added.",
                "The recorded action added the requested material to the submission.",
            )
        if re.fullmatch(r"[\s\W]*(?:noted|acknowledged|ok|will comply)[\s\W]*", lower):
            return (
                "Acknowledged without a traceable revision",
                "The response acknowledged the reviewer comment but did not identify a sheet, detail, document, or completed design change.",
                "The stored response was brief and did not provide a concrete verification location.",
            )
        return (
            "Recorded applicant response",
            "The history contains a confirmed applicant response, but it does not fit a more specific action category.",
            "The response is preserved as project-specific evidence rather than treated as a recurring method.",
        )

    def _structured_evidence(self, rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
        """Return compact summaries with exact evidence hidden one click away."""
        evidence: list[dict[str, Any]] = []
        candidates: list[tuple[int, str, dict[str, Any], dict[str, Any] | None]] = []
        for row in rows:
            response = self._confirmed_response(row["comment_id"])
            level, _reason = self._evidence_level(row, response)
            candidates.append((level, _project_label(row), row, response))
        candidates.sort(key=lambda item: (-item[0], item[1].casefold(), str(item[2].get("comment_id", ""))))
        project_counts: Counter[str] = Counter()
        for level, project, row, response in candidates:
            if project_counts[project] >= 2:
                continue
            view = self.store._view_comment(row)
            # Preserve the existing evidence boundary: only a confirmed
            # response may contribute response-source occurrences.
            response_view = (view.get("response") or {}) if response else {}
            comment_sources = [item for item in view.get("sources", []) if item.get("kind") == "local"]
            response_sources = [item for item in response_view.get("sources", []) if item.get("kind") == "local"]
            comment_source = comment_sources[0] if comment_sources else None
            response_source = response_sources[0] if response_sources else None
            later_event = next((
                event for event in reversed((view.get("issue_thread") or {}).get("events", []))
                if str(event.get("event_type", "")).casefold() == "reviewer_follow_up"
                and _clean_text(event.get("text"), 500)
            ), None)
            later_source = (later_event or {}).get("source") or {}
            response_text = verified_text(response) if response else ""
            _level, reason = self._evidence_level(row, response)
            badge = {
                1: "No confirmed response",
                2: "Response recorded",
                3: "Specific revision cited",
                4: "Later review confirmed",
            }.get(level, "Response recorded")
            event_id = str(row.get("canonical_event_id") or row.get("comment_id"))
            project_id = _project_key(row)
            source_ids = list(dict.fromkeys(
                str(item.get("source_id"))
                for item in [*comment_sources, *response_sources, later_source]
                if isinstance(item, dict) and item.get("source_id")
            ))
            source_occurrences: list[dict[str, Any]] = []
            seen_occurrences: set[str] = set()
            for role, items in (
                ("comment", comment_sources),
                ("response", response_sources),
                ("later_review", [later_source] if isinstance(later_source, dict) else []),
            ):
                for source_item in items:
                    source_id = str(source_item.get("source_id") or "")
                    if not source_id or source_id in seen_occurrences:
                        continue
                    seen_occurrences.add(source_id)
                    source_occurrences.append({
                        "source_id": source_id,
                        "filename": str(source_item.get("filename") or ""),
                        "relation": str(source_item.get("relation") or ""),
                        "role": role,
                        "label": f"{role.replace('_', ' ').title()} source",
                    })
            primary_source = response_source or comment_source or (later_source if isinstance(later_source, dict) else None)
            comment_summary, comment_summary_complete = _complete_excerpt(verified_text(row), 360)
            response_summary, response_summary_complete = _complete_excerpt(response_text, 360)
            comment_excerpt, comment_excerpt_complete = _complete_excerpt(verified_text(row), 1200)
            response_excerpt, response_excerpt_complete = _complete_excerpt(response_text, 1200)
            later_review_excerpt, later_review_excerpt_complete = _complete_excerpt(
                (later_event or {}).get("text"), 1200,
            )
            issue_tags = [
                str(value).strip() for value in row.get("issue_tags", [])
                if str(value).strip()
            ] if isinstance(row.get("issue_tags"), list) else []
            category = str(view.get("category") or row.get("category") or "").strip()
            comment_label = str(view.get("comment_label") or row.get("comment_label") or "").strip()
            explicit_issue_title = str(
                row.get("issue_title") or row.get("canonical_issue_title") or ""
            ).strip()
            if explicit_issue_title:
                issue_label = explicit_issue_title
            elif comment_label:
                issue_label = comment_label
            elif issue_tags:
                # These are persisted/confirmed retrieval labels.  This is a
                # display projection only; it does not classify the event.
                issue_label = " · ".join(
                    value.replace("_", " ").strip().title()
                    for value in issue_tags[:2]
                )
            elif category and category.casefold() != "uncategorized":
                issue_label = category
            else:
                discipline = str(row.get("discipline") or "Historical review").strip()
                number = str(row.get("comment_number") or "").strip()
                issue_label = f"{discipline}{f' · Comment {number}' if number else ''}"
            evidence.append({
                "event_id": event_id,
                "comment_id": str(row.get("comment_id") or ""),
                "issue_id": str(row.get("canonical_issue_id") or row.get("issue_thread_id") or ""),
                "project_id": project_id,
                "claim": (
                    f"{project or row.get('city') or 'This project'} has a confirmed historical response for this issue."
                    if response else f"{project or row.get('city') or 'This project'} contains a relevant reviewer comment without a confirmed response."
                ),
                "project": project,
                "city": row.get("city", ""),
                "round": row.get("review_round", ""),
                "issue_label": _clean_text(issue_label, 160),
                "topic_label": _clean_text(category, 120) if category and category.casefold() != "uncategorized" else "",
                "summary": response_summary or comment_summary,
                "reviewer_summary": comment_summary,
                "response_summary": response_summary,
                "comment_excerpt": comment_excerpt,
                "response_excerpt": response_excerpt,
                "later_review_excerpt": later_review_excerpt,
                "comment_text_complete": comment_excerpt_complete,
                "response_text_complete": response_excerpt_complete,
                "later_review_text_complete": later_review_excerpt_complete,
                "comment_summary_complete": comment_summary_complete,
                "response_summary_complete": response_summary_complete,
                "comment_source_id": comment_source.get("source_id") if comment_source else None,
                "response_source_id": response_source.get("source_id") if response_source else None,
                "later_review_source_id": later_source.get("source_id") if isinstance(later_source, dict) else None,
                "source_ids": source_ids,
                "primary_source_occurrence_id": primary_source.get("source_id") if primary_source else None,
                "source_occurrence_ids": source_ids,
                "source_occurrences": source_occurrences,
                "evidence_level": level,
                "evidence_badge": badge,
                "evidence_level_reason": reason,
            })
            project_counts[project] += 1
            if len(evidence) >= limit:
                break
        return evidence

    @staticmethod
    def _with_inline_citations(
        text: str,
        supporting_event_ids: list[str],
        citation_indexes: dict[str, int],
    ) -> str:
        """Attach stable source markers to one supported narrative claim."""
        clean = str(text or "").strip()
        indexes = list(dict.fromkeys(
            citation_indexes[event_id]
            for event_id in supporting_event_ids
            if event_id in citation_indexes
        ))
        if not clean or not indexes:
            return ""
        clean = re.sub(r"(?:\s*\[\d+\])+\s*$", "", clean).rstrip()
        return f"{clean} {' '.join(f'[{index}]' for index in indexes)}"

    def _deterministic_cited_answer(
        self,
        answer_type: str,
        subject: str,
        evidence: list[dict[str, Any]],
        citation_indexes: dict[str, int],
        metrics: dict[str, int],
    ) -> str:
        """Produce a grounded, pattern-first fallback when synthesis fails.

        This uses only already validated evidence and backend-owned response
        classifications.  It is intentionally more useful than a record list
        while remaining fully deterministic and citation-addressable.
        """
        cited = [item for item in evidence if str(item.get("event_id")) in citation_indexes]
        if not cited:
            return ""
        count = int(metrics.get("parent_comments", 0) or 0)
        project_count = int(metrics.get("projects", 0) or 0)
        round_count = int(metrics.get("review_rounds", 0) or 0)
        response_count = int(metrics.get("confirmed_responses", 0) or 0)
        count_intro = (
            f"I found **{count} relevant {'comment' if count == 1 else 'comments'}** about **{subject}** across "
            f"**{project_count} {'project' if project_count == 1 else 'projects'}** and "
            f"**{round_count} review {'round' if round_count == 1 else 'rounds'}**. "
            f"**{response_count}** {'has' if response_count == 1 else 'have'} a confirmed applicant response."
        )
        with_responses = [item for item in cited if item.get("response_excerpt")]
        if not with_responses:
            statements = []
            for item in cited[:3]:
                text = (
                    f"At **{item.get('project') or item.get('city') or 'this project'}**, "
                    f"the reviewer requested {_clean_text(item.get('reviewer_summary'), 260).rstrip('.')} ."
                ).replace(" .", ".")
                rendered = self._with_inline_citations(
                    text, [str(item.get("event_id"))], citation_indexes,
                )
                if rendered:
                    statements.append(rendered)
            conclusion = (
                "The available history establishes what reviewers raised, but it does not contain a confirmed "
                "applicant response that establishes how those comments were handled."
            )
            if answer_type == "COUNT":
                return "\n\n".join((count_intro, conclusion, " ".join(statements)))
            return "\n\n".join((conclusion, " ".join(statements)))

        pattern_groups: dict[str, list[str]] = {}
        for item in with_responses:
            title, _explanation, _action = self._response_pattern(str(item.get("response_excerpt") or ""))
            pattern_groups.setdefault(title, []).append(str(item.get("event_id")))
        pattern_names = [name.casefold() for name in list(pattern_groups)[:3]]
        if len(pattern_names) == 1:
            approach = pattern_names[0]
        elif len(pattern_names) == 2:
            approach = f"{pattern_names[0]} and {pattern_names[1]}"
        else:
            approach = ", ".join(pattern_names[:-1]) + f", and {pattern_names[-1]}"
        intro_ids = list(dict.fromkeys(
            event_id for ids in pattern_groups.values() for event_id in ids
        ))
        intro = self._with_inline_citations(
            f"Across the representative history, {subject} comments were handled through {approach}.",
            intro_ids,
            citation_indexes,
        )

        by_project: dict[str, list[dict[str, Any]]] = {}
        for item in with_responses:
            by_project.setdefault(str(item.get("project") or item.get("city") or "This project"), []).append(item)
        examples: list[str] = []
        for project, items in list(by_project.items())[:3]:
            summaries: list[str] = []
            for item in items[:2]:
                candidate = str(item.get("response_summary") or "")
                candidate_complete = bool(item.get("response_summary_complete"))
                if not candidate_complete:
                    candidate, candidate_complete = _complete_excerpt(item.get("response_excerpt"), 500)
                if candidate and candidate_complete:
                    summaries.append(candidate.rstrip(" ."))
            summaries = list(dict.fromkeys(summaries))
            if not summaries:
                # Preserve the historical pattern without copying a visibly
                # incomplete evidence fragment into the conversational answer.
                pattern_title, _explanation, historical_action = self._response_pattern(
                    str(items[0].get("response_excerpt") or "")
                )
                summaries = [historical_action.rstrip(" .") or pattern_title.casefold()]
            if len(summaries) == 1:
                body = summaries[0]
            else:
                body = f"{summaries[0]}; the history also records that {summaries[1][0].lower()}{summaries[1][1:]}"
            rendered = self._with_inline_citations(
                f"At **{project}**, {body}.",
                [str(item.get("event_id")) for item in items[:2]],
                citation_indexes,
            )
            if rendered:
                examples.append(rendered)
        body = "\n\n".join(part for part in (intro, " ".join(examples)) if part)
        takeaway_ids = [
            str(item.get("event_id")) for item in with_responses
            if int(item.get("evidence_level", 0) or 0) >= 3
        ]
        takeaway = self._with_inline_citations(
            "The practical pattern is that responses were easiest to verify when they named the revised sheet, detail, plan, calculation, report, or completed design change rather than only acknowledging the comment.",
            takeaway_ids,
            citation_indexes,
        ) if takeaway_ids and answer_type != "COUNT" else ""
        return "\n\n".join(
            part for part in ((count_intro if answer_type == "COUNT" else ""), body, takeaway)
            if part
        )

    def _deterministic_patterns(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            response = self._confirmed_response(row["comment_id"])
            if not response:
                continue
            title, explanation, action = self._response_pattern(verified_text(response))
            group = grouped.setdefault(title, {
                "title": title,
                "explanation": explanation,
                "historical_action": action,
                "supporting_event_ids": [],
                "supporting_project_ids": [],
            })
            group["supporting_event_ids"].append(str(row.get("canonical_event_id") or row.get("comment_id")))
            group["supporting_project_ids"].append(_project_key(row))
        patterns: list[dict[str, Any]] = []
        for group in sorted(grouped.values(), key=lambda item: (-len(set(item["supporting_event_ids"])), item["title"])):
            event_ids = list(dict.fromkeys(group["supporting_event_ids"]))
            project_ids = list(dict.fromkeys(group["supporting_project_ids"]))
            support_level = self._support_level(event_ids, project_ids)
            explanation = group["explanation"]
            if support_level == "single_record":
                explanation = f"In one project, {explanation[0].lower()}{explanation[1:]}"
            patterns.append({
                **group,
                "explanation": explanation,
                "supporting_event_ids": event_ids,
                "supporting_project_ids": project_ids,
                "support_level": support_level,
                "evidence_ids": event_ids,
            })
            if len(patterns) >= 5:
                break
        return patterns

    def _structured_answer(
        self,
        message: str,
        plan: dict[str, Any],
        metrics: dict[str, int],
        rows: list[dict[str, Any]],
        sections: dict[str, str],
        natural_answer: str,
    ) -> dict[str, Any]:
        answer_type = self._presentation_type(message, plan)
        # Every validated answer receives an analytical narrative.  Answer
        # type controls depth: COUNT/FACT_LOOKUP stay concise, while broad
        # history questions may also expose patterns, differences, and a
        # historical takeaway.
        analytical = True
        broad_analysis = answer_type in {"HISTORY_SUMMARY", "HOW_HANDLED", "COMPARISON", "TIMELINE", "PRACTICAL_LESSONS"}
        all_evidence = self._structured_evidence(rows, limit=12) if plan.get("_validation_status") in {"validated", "not_required"} else []
        representative_evidence = all_evidence[:5]
        citation_indexes = {
            str(item.get("event_id")): index
            for index, item in enumerate(representative_evidence, start=1)
            if item.get("event_id") and item.get("primary_source_occurrence_id")
        }
        patterns = self._deterministic_patterns(rows) if broad_analysis else []
        differences: list[dict[str, Any]] = []
        takeaway: dict[str, str] | None = None
        conversational_answer = str(natural_answer or "").strip()

        project_actions: dict[str, set[str]] = {}
        for row in rows:
            response = self._confirmed_response(row["comment_id"])
            if response:
                project_actions.setdefault(_project_label(row), set()).add(self._response_pattern(verified_text(response))[0])
        distinct_actions = {action for actions in project_actions.values() for action in actions}
        if broad_analysis and len(project_actions) >= 2 and len(distinct_actions) >= 2:
            parts = [f"{project}: {', '.join(sorted(actions))}" for project, actions in list(sorted(project_actions.items()))[:4]]
            supporting = [str(row.get("canonical_event_id") or row.get("comment_id")) for row in rows if self._confirmed_response(row["comment_id"])]
            differences.append({
                "title": "Where the projects differed",
                "text": "; ".join(parts) + ".",
                "supporting_event_ids": list(dict.fromkeys(supporting)),
            })
        if broad_analysis and patterns:
            if any(item.get("evidence_level", 0) >= 3 for item in all_evidence):
                takeaway = {
                    "type": "historical_inference",
                    "text": "The history suggests that responses are easier to verify when they name the actual sheet, detail, plan, calculation, report, or completed design change instead of only acknowledging the comment.",
                }
            else:
                takeaway = {
                    "type": "historical_inference",
                    "text": "Based on these examples, a safer response would identify a concrete revision and where the reviewer can verify it.",
                }

        client = self._chat_client()
        # COUNT is analytical too, but its number-bearing lead is assembled
        # locally from authoritative metrics.  Skipping remote prose for this
        # compact type prevents a model from dropping, changing, or repeating
        # the count while the deterministic evidence explanation still gives
        # the user context and cited examples.
        if analytical and answer_type != "COUNT" and all_evidence and client and hasattr(client, "synthesize_knowledge_answer"):
            synthesis_packet = [{
                "citation_index": citation_indexes.get(str(item["event_id"])),
                "event_id": item["event_id"],
                "project_id": item["project_id"],
                "project": item["project"],
                "city": item["city"],
                "round": item["round"],
                "issue_label": item["issue_label"],
                "reviewer_text": item["comment_excerpt"],
                "confirmed_response_text": item["response_excerpt"],
                "later_reviewer_text": item["later_review_excerpt"],
                "reviewer_text_complete": item["comment_text_complete"],
                "confirmed_response_text_complete": item["response_text_complete"],
                "later_reviewer_text_complete": item["later_review_text_complete"],
                "evidence_level": item["evidence_level"],
            } for item in representative_evidence if str(item["event_id"]) in citation_indexes]
            allowed = {item["event_id"]: item for item in representative_evidence}
            try:
                generated = client.synthesize_knowledge_answer(message, answer_type, {
                    "comment_count": metrics.get("parent_comments", 0),
                    "issue_count": metrics.get("canonical_issues", 0),
                    "project_count": metrics.get("projects", 0),
                    "round_count": metrics.get("review_rounds", 0),
                    "confirmed_response_count": metrics.get("confirmed_responses", 0),
                    "missing_response_count": metrics.get("missing_responses", 0),
                }, synthesis_packet)
                generated_blocks: list[str] = []
                allowed_fact_keys = {
                    "comment_count", "issue_count", "project_count", "round_count",
                    "confirmed_response_count", "missing_response_count",
                }
                for block in generated.get("answer_blocks", [])[:8]:
                    event_ids = list(dict.fromkeys(
                        str(value) for value in block.get("supporting_event_ids", [])
                        if str(value) in allowed
                    ))
                    fact_keys = list(dict.fromkeys(
                        str(value) for value in block.get("backend_fact_keys", [])
                        if str(value) in allowed_fact_keys
                    ))
                    clean_block = _clean_text(block.get("text"), 1200)
                    rendered = self._with_inline_citations(clean_block, event_ids, citation_indexes)
                    if not rendered and clean_block and fact_keys:
                        rendered = clean_block
                    if rendered:
                        generated_blocks.append(rendered)
                if generated_blocks:
                    conversational_answer = "\n\n".join(generated_blocks)
                else:
                    generated_answer = _clean_text(generated.get("answer"), 4000)
                    valid_markers = {
                        int(value) for value in re.findall(r"\[(\d+)\]", generated_answer)
                        if int(value) in citation_indexes.values()
                    }
                    if generated_answer and valid_markers:
                        conversational_answer = generated_answer
                generated_patterns: list[dict[str, Any]] = []
                for item in generated.get("patterns", [])[:5]:
                    event_ids = list(dict.fromkeys(str(value) for value in item.get("supporting_event_ids", []) if str(value) in allowed))
                    if not event_ids:
                        continue
                    project_ids = list(dict.fromkeys(allowed[event_id]["project_id"] for event_id in event_ids))
                    generated_patterns.append({
                        "title": _clean_text(item.get("title"), 120),
                        "explanation": _clean_text(item.get("explanation"), 600),
                        "historical_action": _clean_text(item.get("historical_action"), 500),
                        "supporting_event_ids": event_ids,
                        "supporting_project_ids": project_ids,
                        "support_level": self._support_level(event_ids, project_ids),
                        "evidence_ids": event_ids,
                    })
                if generated_patterns and broad_analysis:
                    patterns = generated_patterns
                generated_differences: list[dict[str, Any]] = []
                for item in generated.get("differences", [])[:3]:
                    event_ids = list(dict.fromkeys(str(value) for value in item.get("supporting_event_ids", []) if str(value) in allowed))
                    if event_ids and _clean_text(item.get("text"), 700):
                        generated_differences.append({
                            "title": _clean_text(item.get("title"), 120) or "Where the records differed",
                            "text": _clean_text(item.get("text"), 700),
                            "supporting_event_ids": event_ids,
                        })
                if generated_differences and broad_analysis:
                    differences = generated_differences
                generated_takeaway = _clean_text(generated.get("takeaway"), 700)
                if generated_takeaway and broad_analysis:
                    if not re.match(r"^(?:the history suggests|based on (?:these|the) (?:examples|records)|the records indicate|a safer approach)", generated_takeaway.casefold()):
                        generated_takeaway = f"The history suggests that {generated_takeaway[0].lower()}{generated_takeaway[1:]}"
                    takeaway = {"type": "historical_inference", "text": generated_takeaway}
            except (RuntimeError, TypeError, ValueError, AttributeError):
                self._mark_remote_failure()

        # A useful analytical answer needs a traceable example whenever
        # representative source evidence exists.  This also replaces the old
        # record-by-record fallback when Gemini is unavailable or incomplete.
        if analytical and representative_evidence and (answer_type == "COUNT" or not re.search(r"\[\d+\]", conversational_answer)):
            cited_fallback = self._deterministic_cited_answer(
                answer_type,
                str(plan.get("subject") or "the requested topic"),
                representative_evidence,
                citation_indexes,
                metrics,
            )
            if cited_fallback:
                conversational_answer = cited_fallback

        # Keep the old array for API compatibility, but make it the paragraph
        # split of the single conversational answer. The UI renders `answer`
        # as one coherent block and does not expose internal pattern cards.
        direct_answer = [part.strip() for part in re.split(r"\n\s*\n", conversational_answer) if part.strip()]

        limitations: list[str] = []
        if rows and metrics.get("projects", 0) <= 2:
            limitations.append(
                f"This is a relatively small history: {metrics.get('parent_comments', 0)} relevant comments across {metrics.get('projects', 0)} projects. Treat it as useful precedent rather than a universal citywide practice."
            )
        missing = metrics.get("missing_responses", 0)
        unconfirmed = metrics.get("unconfirmed_responses", 0)
        if missing or unconfirmed:
            limitations.append(
                f"{missing} comments have no stored response and {unconfirmed} have an unconfirmed response link; those records were not used to infer applicant actions."
            )
        if not limitations and self._without_label(sections.get("data_limitation", "")):
            limitations.append(self._without_label(sections.get("data_limitation", "")))

        coverage = {
            # Keep the legacy names while exposing the new authoritative schema.
            "comments": metrics.get("parent_comments", 0),
            "projects": metrics.get("projects", 0),
            "review_rounds": metrics.get("review_rounds", 0),
            "confirmed_responses": metrics.get("confirmed_responses", 0),
            "comment_count": metrics.get("parent_comments", 0),
            "issue_count": metrics.get("canonical_issues", 0) or metrics.get("parent_comments", 0),
            "project_count": metrics.get("projects", 0),
            "round_count": metrics.get("review_rounds", 0),
            "confirmed_response_count": metrics.get("confirmed_responses", 0),
            "missing_response_count": metrics.get("missing_responses", 0),
        }
        return {
            "answer_type": answer_type,
            "answer": conversational_answer,
            "direct_answer": direct_answer,
            "key_patterns": patterns,
            "patterns": patterns,
            "differences": differences,
            "takeaway": takeaway,
            "evidence": representative_evidence,
            "representative_evidence": representative_evidence,
            "coverage": coverage,
            "limitations": limitations,
            "suggested_followups": [],
            "uncertainty": limitations[0] if limitations else "",
        }

    def _guided_plan(self, action: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        """Build a safe plan for a UI action generated from a prior result set.

        Actions are capability-driven and allowlisted.  The button label is
        presentation only; the backend decides what operation is actually
        executed and which result set may be used as its scope.
        """
        action_type = _clean_text(action.get("type"), 80)
        if action_type not in GUIDED_ACTION_TYPES:
            raise PlanValidationError("Unsupported guided exploration action")
        parameters = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
        topic = _clean_text(parameters.get("topic"), 300)
        previous_query = _clean_text((previous or {}).get("query"), 500)
        subject = topic or previous_query or "the selected result set"
        if action_type == "filter_subtopic":
            intent, operations = "filter_previous_results", ["load_previous_result_set"]
        elif action_type == "broaden_scope":
            # The city filter is inherited below from the prior result set.
            # This changes retrieval depth, never geographic scope.
            intent, operations = "precedent_search", ["smart_search"]
        elif action_type == "compare_projects":
            intent, operations = "compare_groups", ["load_previous_result_set", "group_by_project"]
        elif action_type == "timeline_analysis":
            intent, operations = "topic_summary", ["load_previous_result_set", "group_by_review_round"]
        elif action_type == "response_analysis":
            intent, operations = "historical_response_summary", ["load_previous_result_set", "summarize_confirmed_responses"]
        elif action_type == "unresolved_analysis":
            intent, operations = "topic_summary", ["load_previous_result_set", "group_by_response_status"]
        else:
            intent, operations = "precedent_search", ["smart_search"]
        return validate_query_plan({
            "intent": intent,
            "subject": subject,
            "operations": operations,
            "filters": {},
            "needs_clarification": not bool(previous),
            "clarification_question": "This exploration result has expired. Please run the broader question again first." if not previous else "",
        })

    def _guided_actions(
        self,
        result_set_id: str,
        plan: dict[str, Any],
        metrics: dict[str, int],
        rows: list[dict[str, Any]],
        validation_status: str,
    ) -> list[dict[str, Any]]:
        """Return at most four evidence-aware next steps for the current set."""
        if not rows or validation_status in {"no_validated_evidence", "insufficient_comparison", "unverified"}:
            return []
        base = {"result_set_id": result_set_id}
        actions: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(action_type: str, label: str, parameters: dict[str, Any] | None = None) -> None:
            key = (action_type, label)
            if len(actions) >= 4 or key in seen:
                return
            seen.add(key)
            params = dict(base)
            if parameters:
                params.update(parameters)
            actions.append({
                "type": action_type,
                "label": label,
                "result_set_id": result_set_id,
                "parameters": params,
            })

        # Put the highest-value analytical directions first.  Topic cards are
        # appended afterwards so noisy or numerous topic labels cannot crowd
        # out timeline, response, and unresolved analysis.
        if metrics.get("projects", 0) >= 2:
            add("compare_projects", "Compare these issues across projects")
        if metrics.get("review_rounds", 0) >= 2:
            add("timeline_analysis", "See what repeated across review rounds")
        if metrics.get("confirmed_responses", 0) > 0:
            add("response_analysis", "Compare how applicants responded")
        if metrics.get("missing_responses", 0) > 0 or metrics.get("unconfirmed_responses", 0) > 0:
            add("unresolved_analysis", "Show issues without a confirmed resolution")

        # Topic cards are useful only when there is a meaningful label.  The
        # common-topic helper already operates on canonical issue occurrences,
        # so these suggestions do not reintroduce physical-file duplicates.
        try:
            _, topics = self.store._common_topics(rows, limit=3)
        except (AttributeError, TypeError, ValueError):
            topics = []
        for topic in topics:
            label = _clean_text(topic.get("label"), 120)
            if len(label) >= 8 and label.casefold() not in {"a", "comment", "comments", "additional design requirements"}:
                add("filter_subtopic", f"Explore: {label}", {"topic": label})
        return actions

    def chat(self, request: dict[str, Any]) -> dict[str, Any]:
        self._purge()
        self._last_progressive_result = {}
        message = _clean_text(request.get("message"), 4000)
        if not message:
            raise ValueError("Knowledge-chat message is required")
        conversation_id = _clean_text(request.get("conversation_id"), 120) or f"conv_{secrets.token_hex(8)}"
        previous_id = _clean_text(request.get("previous_result_set_id"), 120)
        selected_comment_id = _clean_text(request.get("selected_comment_id"), 160)
        guided_action = request.get("guided_action") if isinstance(request.get("guided_action"), dict) else None
        if guided_action:
            action_result_id = _clean_text(guided_action.get("result_set_id") or (guided_action.get("parameters") or {}).get("result_set_id"), 120)
            if action_result_id:
                previous_id = action_result_id
        previous = self.result_sets.get(previous_id) if previous_id else None
        if previous_id and not previous:
            raise KeyError("Previous result set was not found or has expired")
        if guided_action:
            plan, warnings = self._guided_plan(guided_action, previous), []
        else:
            plan, warnings = self._route(message, bool(previous))
        # Keep a model paraphrase only if it remains anchored to the words in
        # the user's question.  This prevents a stale or hallucinated router
        # subject from sending retrieval toward an unrelated discipline.
        if not guided_action and plan.get("intent") != "explain_selected_comment":
            plan["subject"] = self._local_subject(message, plan.get("subject", ""))
        # Questions asking how an issue was handled require comment-response
        # histories, not a corpus-wide topic breakdown.  Keep this intent
        # override local and deterministic so a weak router cannot turn a
        # conversational question into an audit report.
        lower_message = message.casefold()
        if re.search(r"\b(separate|distinguish|differentiate|split)\b", lower_message) and "door" in lower_message and re.search(r"\b(size|narrow|width|rating|rated)\b", lower_message):
            plan["intent"] = "compare_groups"
            plan["operations"] = ["smart_search", "summarize_confirmed_responses"]
            plan["subject"] = "door size and door rating"
            plan["comparison_subject_groups"] = ["door size", "door rating"]
        asks_for_handling = bool(re.search(
            r"\b(how (?:have|has|did) we|how was|how were|handled|addressed|responded|confirmed responses?)\b",
            lower_message,
        ))
        if asks_for_handling and plan["intent"] not in {"explain_selected_comment", "filter_previous_results", "aggregate_count"}:
            plan["intent"] = "historical_response_summary"
            plan["operations"] = ["smart_search", "summarize_confirmed_responses"]
        if (
            plan["intent"] == "historical_response_summary"
            and (
                re.search(r"\bconfirmed responses?\b", lower_message)
                or re.search(r"\b(?:show|find|give)\b.*\bexamples?\b", lower_message)
            )
        ):
            # Example requests make an evidentiary claim about the response,
            # so literal fallback rows must not be presented as confirmed.
            plan["_requires_response_verification"] = True
        request_filters = request.get("filters", {}) if isinstance(request.get("filters", {}), dict) else {}
        filters = {key: _clean_text(value, 120) for key, value in request_filters.items() if key in ALLOWED_FILTERS and _clean_text(value, 120)}
        city = _clean_text(request.get("city_id"), 120)
        named_cities = [item["name"] for item in self.store.cities() if item["name"].casefold() in message.casefold()]
        if city and not (plan["intent"] == "compare_groups" and len(named_cities) >= 2):
            filters["city"] = city
        filters.update(self._validated_model_filters(plan["filters"]))
        # A follow-up inherits the prior validated scope unless the caller
        # explicitly supplies a replacement filter.  ``broaden_scope`` keeps
        # that scope while widening retrieval stage; it does not silently
        # expand city/project boundaries.
        if previous and not request_filters:
            prior_filters = previous.get("filters") if isinstance(previous.get("filters"), dict) else {}
            inherited = {
                key: _clean_text(value, 120)
                for key, value in prior_filters.items()
                if key in ALLOWED_FILTERS and _clean_text(value, 120)
            }
            inherited.update(filters)
            filters = inherited
        plan = enrich_query_plan(plan, message, bool(previous), filters)
        plan["filters"] = filters

        missing_selected = plan["intent"] == "explain_selected_comment" and selected_comment_id not in self.store._comments_by_id
        if plan["needs_clarification"] or (plan["intent"] == "filter_previous_results" and not previous) or missing_selected:
            question = plan["clarification_question"] or ("Select a comment first, then ask me to explain it." if missing_selected else "Which previous result set should I filter?")
            return {
                "conversation_id": conversation_id, "answer": question, "answer_sections": {},
                "intent": "unsupported_or_ambiguous", "result_set_id": None, "metrics": {},
                "citations": [], "actions": [], "warnings": warnings, "query_plan": plan,
                "needs_clarification": True,
            }

        classes: dict[str, str] = {}
        if plan["intent"] == "explain_selected_comment":
            comment_ids = [selected_comment_id]
            classes = {selected_comment_id: "direct"}
        elif previous and "load_previous_result_set" in plan["operations"]:
            comment_ids = list(previous["comment_ids"])
            classes = dict(previous["match_classes"])
            lower = message.casefold()
            action_type = _clean_text(guided_action.get("type"), 80) if guided_action else ""
            action_parameters = guided_action.get("parameters") if guided_action and isinstance(guided_action.get("parameters"), dict) else {}
            if action_type == "filter_subtopic":
                topic = _clean_text(action_parameters.get("topic"), 300)
                comment_ids = [item for item in comment_ids if self._candidate_matches_subject(topic, self.store._comments_by_id[item])]
            elif action_type == "unresolved_analysis":
                comment_ids = [item for item in comment_ids if not self._confirmed_response(item)]
            elif action_type == "response_analysis":
                comment_ids = [item for item in comment_ids if self._confirmed_response(item)]
            elif "without response" in lower:
                filters["response_status"] = "missing"
            elif "confirmed response" in lower or "how did we respond" in lower:
                filters["response_status"] = "confirmed"
            comment_ids = [item for item in comment_ids if self._record_matches_filters(self.store._comments_by_id[item], filters)]
        elif self._scope_overview(message, plan):
            plan["operations"] = list(dict.fromkeys([
                operation for operation in plan["operations"] if operation != "smart_search"
            ] + ["load_filtered_comments", "group_by_discipline", "group_by_response_status"]))
            comment_ids = [row["comment_id"] for row in self.store._comments if self._record_matches_filters(row, filters)]
            classes = {item: "direct" for item in comment_ids}
        elif "smart_search" in plan["operations"]:
            # A comparison may still report the size of a literal candidate
            # pool when Gemini validation is unavailable, but those rows must
            # remain explicitly unverified and cannot produce citations or
            # historical conclusions.  Handling questions keep the stricter
            # behaviour and return no literal candidates in that situation.
            comment_ids, classes, search_warnings = self._smart_ids(
                plan["subject"] or message,
                filters,
                intent=plan["intent"],
                allow_unverified=plan["intent"] == "compare_groups",
                force_stage3=bool(guided_action and _clean_text(guided_action.get("type"), 80) == "broaden_scope"),
            )
            if self._last_progressive_result:
                plan["_progressive_retrieval"] = self._last_progressive_result
            warnings.extend(search_warnings)
            if comment_ids and all(classes.get(item) == "unverified" for item in comment_ids):
                plan["evidence_scope"] = "literal_unverified"
        else:
            comment_ids = self._keyword_ids(plan["subject"] or message, filters)
            classes = {item: "direct" for item in comment_ids}

        # Apply the evidence gate once more after every retrieval path,
        # including selected-comment and conversation follow-ups.  This keeps
        # a previously cached result from reintroducing a needs_review row.
        comment_ids = list(dict.fromkeys(
            item for item in comment_ids
            if item in self.store._comments_by_id
            and self._record_matches_filters(self.store._comments_by_id[item], filters)
        ))
        raw_rows = [self.store._comments_by_id[item] for item in comment_ids]
        candidate_metrics = self._metrics(comment_ids)
        excluded_records: list[dict[str, Any]] = []
        validation_warnings: list[str] = []
        validation_status = "not_required"
        # Topic validation is deliberately applied before metrics, citations,
        # summaries, and result-set creation.  This prevents an off-topic
        # retrieval from becoming authoritative merely because it has a
        # confirmed response.
        topic_validation_required = (
            plan["intent"] in {"precedent_search", "historical_response_summary", "topic_summary", "compare_groups", "aggregate_count"}
            and (
                "smart_search" in plan["operations"]
                or plan["intent"] == "compare_groups"
                or (plan["intent"] == "aggregate_count" and bool(_controlled_topic(plan["subject"] or message)))
            )
            and not (guided_action and _clean_text(guided_action.get("type"), 80) != "broaden_scope")
        )
        rows = raw_rows
        if topic_validation_required and rows:
            rows, excluded_records, validation_warnings, validation_status = self._validate_retrieved_rows(
                plan["subject"] or message,
                raw_rows,
                local_fallback_intent=plan.get("intent", ""),
            )
            comment_ids = [row["comment_id"] for row in rows]
            warnings.extend(validation_warnings)
            if excluded_records:
                warnings.append(f"Excluded {len(excluded_records)} retrieved record{'s' if len(excluded_records) != 1 else ''} as off-topic before answer generation.")
        direct_ids = [item for item in comment_ids if classes.get(item) == "direct"]
        related_ids = [item for item in comment_ids if classes.get(item) == "related"]
        metrics = self._metrics(comment_ids)
        if topic_validation_required:
            relevant_projects = metrics["projects"]
            if plan["intent"] == "compare_groups" and relevant_projects < 2 and not plan.get("comparison_subject_groups"):
                validation_status = "insufficient_comparison"
            elif not rows:
                validation_status = "no_validated_evidence"
            elif (
                validation_status == "validated"
                and plan.get("evidence_scope") == "literal_unverified"
                and (
                    plan.get("intent") == "compare_groups"
                    or plan.get("_requires_response_verification")
                    or plan.get("intent") == "topic_summary"
                    or _controlled_topic(plan.get("subject", "")) is not None
                )
            ):
                validation_status = "unverified"
        plan["_validation_status"] = validation_status
        plan["_excluded_records"] = excluded_records
        plan["_candidate_metrics"] = candidate_metrics
        breakdowns = self._breakdowns(rows)
        now = self.clock()
        result_set_id = f"rs_{int(now)}_{secrets.token_hex(4)}"
        result_set = {
            "result_set_id": result_set_id, "conversation_id": conversation_id,
            "query": message, "intent": plan["intent"], "filters": filters,
            "comment_ids": comment_ids, "direct_comment_ids": direct_ids,
            "related_comment_ids": related_ids, "match_classes": classes,
            "canonical_issue_ids": sorted({str(row.get("canonical_issue_id")) for row in rows if row.get("canonical_issue_id")}),
            "created_at": now, "expires_at": now + self.ttl_seconds,
        }
        if guided_action:
            result_set["parent_result_set_id"] = previous_id or None
            result_set["guided_action"] = _clean_text(guided_action.get("type"), 80)
        self.result_sets[result_set_id] = result_set
        if validation_status in {"insufficient_comparison", "no_validated_evidence"}:
            plan["evidence_scope"] = "validated_insufficient"
        sections = self._answer(message, plan, metrics, rows, breakdowns)
        natural_answer = self._natural_answer(message, plan, metrics, rows, sections)
        authoritative_metrics = metrics
        authoritative_breakdowns = breakdowns
        if validation_status == "unverified":
            # Candidate counts remain available explicitly in
            # validation_summary, but the normal evidence counters must stay
            # at zero so the UI cannot present off-topic candidates as facts.
            authoritative_metrics = {key: 0 for key in metrics}
            authoritative_breakdowns = {}
        structured = self._structured_answer(
            message,
            plan,
            authoritative_metrics,
            rows if validation_status != "unverified" else [],
            sections,
            natural_answer,
        )
        structured_citations: list[dict[str, Any]] = []
        for index, item in enumerate(structured.get("representative_evidence") or [], start=1):
            primary_source_id = str(item.get("primary_source_occurrence_id") or "")
            if not primary_source_id:
                continue
            structured_citations.append({
                "citation_id": f"citation-{index}",
                "citation_index": index,
                "evidence_id": str(item.get("event_id") or ""),
                "comment_id": str(item.get("comment_id") or ""),
                "source_id": primary_source_id,
                "role": "response" if item.get("response_source_id") == primary_source_id else "later_review" if item.get("later_review_source_id") == primary_source_id else "comment",
                "primary_source_occurrence_id": primary_source_id,
                "source_occurrence_ids": list(item.get("source_occurrence_ids") or [primary_source_id]),
                "label": f"{item.get('project') or item.get('city') or 'Historical evidence'} · {item.get('evidence_badge') or 'Source evidence'}",
            })
        action_label = (
            f"View {metrics['parent_comments']} validated comment{'s' if metrics['parent_comments'] != 1 else ''}"
            if validation_status in {"validated", "not_required"}
            else f"View {candidate_metrics['parent_comments']} screened candidate{'s' if candidate_metrics['parent_comments'] != 1 else ''}"
        )
        response = {
            "conversation_id": conversation_id, "answer": natural_answer,
            "answer_sections": sections, "intent": plan["intent"], "result_set_id": result_set_id,
            "metrics": authoritative_metrics,
            "citations": structured_citations if validation_status in {"validated", "not_required"} else [],
            **structured,
            "breakdowns": authoritative_breakdowns,
            "validation_status": validation_status,
            "validation_summary": {
                "relevant_comments": metrics["parent_comments"] if validation_status in {"validated", "not_required", "insufficient_comparison"} else 0,
                "relevant_projects": metrics["projects"] if validation_status in {"validated", "not_required", "insufficient_comparison"} else 0,
                "excluded_off_topic": len(excluded_records),
                "candidate_comments": candidate_metrics["parent_comments"],
                "candidate_projects": candidate_metrics["projects"],
                "candidate_review_rounds": candidate_metrics["review_rounds"],
            },
            "excluded_records": excluded_records,
            "retrieval": {
                "stage": int(plan.get("_progressive_retrieval", {}).get("retrieval_stage_used", 0) or 0),
                "coverage": plan.get("_progressive_retrieval", {}).get("coverage", {}),
                "candidate_coverage": plan.get("_progressive_retrieval", {}).get("candidate_coverage", {}),
                "matched_tags": plan.get("_progressive_retrieval", {}).get("matched_tags", {}),
                "suggested_tags": plan.get("_progressive_retrieval", {}).get("suggested_tags", []),
            },
            "actions": ([{"type": "show_results", "label": action_label, "result_set_id": result_set_id}] if comment_ids and validation_status not in {"no_validated_evidence", "insufficient_comparison"} else []),
            "warnings": warnings, "query_plan": plan, "needs_clarification": False,
        }
        if validation_status in {"validated", "not_required"}:
            response["actions"] = response["actions"] + self._guided_actions(result_set_id, plan, metrics, rows, validation_status)
        # An explicit wider search inspects every verified canonical event in
        # this answer's city. It does not expand to other cities.
        if int(response["retrieval"].get("stage") or 0) != 3:
            city_label = filters.get("city") or "selected city"
            response["actions"].append({
                "type": "broaden_scope",
                "label": f"Search broader {city_label} history (may take longer)",
                "result_set_id": result_set_id,
                "parameters": {"result_set_id": result_set_id, "scope": "city", "may_take_longer": True},
            })
        response["suggested_followups"] = [
            item["label"] for item in response.get("actions", [])
            if item.get("type") != "show_results"
        ]
        conversation = self.conversations.setdefault(conversation_id, {"conversation_id": conversation_id, "messages": []})
        conversation["messages"].extend([
            {"role": "user", "content": message, "created_at": now},
            {"role": "assistant", "content": response["answer"], "result_set_id": result_set_id, "created_at": now},
        ])
        conversation["current_result_set_id"] = result_set_id
        conversation["filters"] = filters
        conversation["selected_comment_id"] = selected_comment_id or None
        return response

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        if conversation_id not in self.conversations:
            raise KeyError("Conversation was not found")
        return self.conversations[conversation_id]

    def result_comments(self, result_set_id: str) -> dict[str, Any]:
        self._purge()
        result = self.result_sets.get(result_set_id)
        if not result:
            raise KeyError("Result set was not found or has expired")
        return {
            "result_set": result,
            "comments": [self.store._view_comment(self.store._comments_by_id[item]) for item in result["comment_ids"]],
        }
