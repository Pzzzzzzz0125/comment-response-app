"""Shared, deterministic identity helpers for comment/event deduplication.

This module deliberately contains no I/O or AI calls.  It is used by both the
historical repair command and the live ingestion/search path so a formatting
artifact cannot create a second canonical event in one path but not the other.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any


# v4 separates canonical event identity from source-observation metadata.  The
# text normalizer remains deterministic and local-only so a repair never needs
# to re-submit the original documents to Gemini.
NORMALIZATION_VERSION = "normalization_v5"
HIGH_CONFIDENCE_SEQUENCE_THRESHOLD = 0.965
HIGH_CONFIDENCE_CONTAINMENT_THRESHOLD = 0.96
HIGH_CONFIDENCE_JACCARD_THRESHOLD = 0.90
HIGH_CONFIDENCE_LENGTH_RATIO = 0.84
POSSIBLE_DUPLICATE_SEQUENCE_THRESHOLD = 0.90
NEGATION_WORDS = frozenset({
    "no", "not", "without", "except", "exclude", "excluded", "prohibit",
    "prohibited", "remain", "remains", "missing", "cannot", "can't",
})
PROGRESSION_PHRASES = (
    "comment remains", "still required", "not addressed", "not resolved",
    "previous response", "does not address", "did not address",
    "provide clarification", "remains unsatisfied", "remain unsatisfied",
)


def _strip_export_prefix(value: str) -> str:
    """Remove only known extraction metadata from the identity text."""
    text = value
    # A cumulative export can prepend the same metadata more than once.
    for _ in range(8):
        before = text
        text = re.sub(
            r"^\s*(?:markup|comment)\s+.*?\bv\s*\d+\s*[-/]?\s*c\s*\d+\s+\d+(?:\.\d+)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        # Keep a compact PC marker only as a round signal; it is not part of
        # the substantive requirement.  This loop handles ``PC1: PC1: ...``.
        text = re.sub(r"^\s*\(?[a-z]\)?\s*pc\s*\d+\s*[-:]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^\s*pc\s*\d+\s*[-:]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^\s*\(?[a-z]\)?\s*[.:]\s*", "", text)
        text = re.sub(r"^\s*\d+\s*[.)]\s+", "", text)
        if text == before:
            break
    return text


def normalize_event_text(value: Any) -> str:
    """Return the stable identity text; never use this to overwrite source text.

    The tokenizer removes punctuation/spacing noise but keeps words, numbers,
    code sections, sheet IDs and measurements as tokens.  Those meaningful
    numeric tokens are separately included in the fingerprint.
    """
    text = html.unescape(unicodedata.normalize("NFKC", str(value or "")))
    text = re.sub(r"(?:_x000[dDaA]_|\*x000[dDaA]\*)", " ", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"-{8,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _strip_export_prefix(text).casefold()
    # Preserve compound technical tokens (1/2, A3.04, CBC-715) while
    # discarding punctuation that differs between OCR/text exports.
    tokens = re.findall(r"[a-z0-9]+(?:[./'’\"-][a-z0-9]+)*", text)
    return " ".join(tokens)


def normalize_actor(value: Any) -> str:
    """Normalize a person/role label for event identity.

    Actor spelling and whitespace vary between workbook exports.  An empty
    actor is intentionally kept empty: missing metadata must not be turned
    into a fabricated actor and can still match a populated copy when the
    other identity fields are strong.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = html.unescape(text).replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def normalize_event_type(value: Any) -> str:
    """Return stable event-type aliases used by all projections."""
    value = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "current_applicant_response": "applicant_response",
        "company_response": "applicant_response",
        "applicant": "applicant_response",
        "government": "government_comment",
        "reviewer_comment": "government_comment",
        "reviewer_followup": "reviewer_follow_up",
        "follow_up": "reviewer_follow_up",
        "note": "discussion_note",
    }
    return aliases.get(value, value or "unknown")


def parameter_tokens(value: Any) -> frozenset[str]:
    """Return meaningful numeric/code/sheet tokens without rewriting text."""
    normalized = normalize_event_text(value)
    return frozenset(
        token for token in normalized.split()
        if re.search(r"\d", token)
    )


def negation_tokens(value: Any) -> frozenset[str]:
    return frozenset(
        token for token in normalize_event_text(value).split()
        if token in NEGATION_WORDS
    )


def text_similarity(left: Any, right: Any) -> float:
    left_text = normalize_event_text(left)
    right_text = normalize_event_text(right)
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def _comparison_tokens(value: Any) -> list[str]:
    """Tokenize for overlap scoring without weakening technical safeguards.

    Hyphen and punctuation differences are harmless for overlap scoring, while
    :func:`parameter_tokens` still compares the original normalized technical
    tokens so dimensions, sheet references, and code sections remain material.
    """
    return re.findall(r"[a-z0-9]+", normalize_event_text(value))


def text_similarity_signals(left: Any, right: Any) -> dict[str, float]:
    """Return the independent text-overlap signals used by canonicalization."""
    left_text = normalize_event_text(left)
    right_text = normalize_event_text(right)
    left_tokens = _comparison_tokens(left)
    right_tokens = _comparison_tokens(right)
    if not left_text or not right_text or not left_tokens or not right_tokens:
        return {
            "sequence": 0.0, "containment": 0.0, "jaccard": 0.0,
            "length_ratio": 0.0,
        }
    left_counts, right_counts = Counter(left_tokens), Counter(right_tokens)
    shared = sum(
        min(left_counts[token], right_counts[token])
        for token in left_counts.keys() | right_counts.keys()
    )
    union = sum(
        max(left_counts[token], right_counts[token])
        for token in left_counts.keys() | right_counts.keys()
    )
    return {
        "sequence": SequenceMatcher(None, left_text, right_text).ratio(),
        "containment": shared / max(1, min(len(left_tokens), len(right_tokens))),
        "jaccard": shared / max(1, union),
        "length_ratio": min(len(left_tokens), len(right_tokens)) / max(
            1, max(len(left_tokens), len(right_tokens))
        ),
    }


def has_progression_language(value: Any) -> bool:
    normalized = normalize_event_text(value)
    return any(phrase in normalized for phrase in PROGRESSION_PHRASES)


def classify_event_text_match(left: Any, right: Any) -> tuple[str, dict[str, float]]:
    """Classify two bodies without considering issue/date/role metadata.

    This is deliberately stricter than topic similarity.  It recognizes exact
    copies and minor OCR/export variation, but refuses automatic equivalence
    when numeric/code parameters or negations differ.  Context checks remain
    the caller's responsibility.
    """
    left_normalized = normalize_event_text(left)
    right_normalized = normalize_event_text(right)
    signals = text_similarity_signals(left, right)
    if not left_normalized or not right_normalized:
        return "DISTINCT", signals
    if left_normalized == right_normalized:
        return "EXACT_DUPLICATE", signals
    if parameter_tokens(left) != parameter_tokens(right):
        return "DISTINCT", signals
    if negation_tokens(left) != negation_tokens(right):
        return "DISTINCT", signals
    # A copied event may contain the same progression wording, but adding or
    # removing progression language generally represents a genuine reissue.
    if has_progression_language(left) != has_progression_language(right):
        return "REISSUE", signals
    if (
        signals["sequence"] >= HIGH_CONFIDENCE_SEQUENCE_THRESHOLD
        and signals["containment"] >= HIGH_CONFIDENCE_CONTAINMENT_THRESHOLD
        and signals["jaccard"] >= HIGH_CONFIDENCE_JACCARD_THRESHOLD
        and signals["length_ratio"] >= HIGH_CONFIDENCE_LENGTH_RATIO
    ):
        return "HIGH_CONFIDENCE_DUPLICATE", signals
    if signals["sequence"] >= POSSIBLE_DUPLICATE_SEQUENCE_THRESHOLD:
        return "POSSIBLE_DUPLICATE", signals
    return "DISTINCT", signals


def canonical_event_fingerprint(
    *,
    site_id: Any,
    role_family: Any,
    effective_round: Any,
    printed_comment_id: Any,
    text: Any,
    event_date: Any = "",
    actor: Any = "",
    issue_id: Any = "",
) -> str:
    """Build a canonical-event fingerprint.

    ``printed_comment_id`` and ``effective_round`` are retained as context for
    older callers, but are not allowed to turn a copied source snapshot into a
    new event when a reliable date is present.  Submission/file/page/source
    identifiers are deliberately absent: those belong to occurrences.
    """
    normalized = normalize_event_text(text)
    parameters = ",".join(sorted(parameter_tokens(text)))
    negations = ",".join(sorted(negation_tokens(text)))
    date = str(event_date or "").strip().casefold()
    round_context = "" if date else str(effective_round or "unknown").strip().casefold()
    fields = (
        str(site_id or "").strip().casefold(),
        normalize_event_type(role_family),
        normalize_actor(actor),
        str(issue_id or "").strip().casefold(),
        round_context,
        date or "unknown",
        normalized,
        parameters,
        negations,
    )
    return "|".join(fields)


def compatible_near_duplicate(
    *,
    left_text: Any,
    right_text: Any,
    left_parameters: Any = None,
    right_parameters: Any = None,
    left_negations: Any = None,
    right_negations: Any = None,
    threshold: float = 0.92,
) -> bool:
    """Conservative fuzzy check for a review-queue candidate.

    This function never authorizes a merge.  It only identifies pairs that
    deserve human review when exact fingerprints do not match.
    """
    left_params = frozenset(left_parameters or parameter_tokens(left_text))
    right_params = frozenset(right_parameters or parameter_tokens(right_text))
    left_neg = frozenset(left_negations or negation_tokens(left_text))
    right_neg = frozenset(right_negations or negation_tokens(right_text))
    return (
        left_params == right_params
        and left_neg == right_neg
        and text_similarity(left_text, right_text) >= threshold
    )


def high_confidence_text_extension(
    shorter: Any,
    longer: Any,
    *,
    threshold: float = 0.65,
) -> bool:
    """Recognize a safe extraction enrichment, not a semantic reissue.

    A frequent parser artifact is a short response being extracted from the
    same cell as a second, complete copy that adds ``see Sheet...`` or page /
    detail references.  Merge only when the shorter token sequence is a
    prefix/contiguous prefix of the longer text, all meaningful parameters of
    the shorter text are preserved, and the added material looks like source
    location/evidence detail.  This deliberately does *not* merge changed
    dimensions, code sections, negations, or arbitrary near-text.
    """
    left = normalize_event_text(shorter)
    right = normalize_event_text(longer)
    if not left or not right or left == right:
        return False
    if len(left.split()) >= len(right.split()):
        return False
    left_tokens = left.split()
    right_tokens = right.split()
    if right_tokens[: len(left_tokens)] != left_tokens:
        return False
    if not set(parameter_tokens(shorter)).issubset(parameter_tokens(longer)):
        return False
    if not set(negation_tokens(shorter)).issubset(negation_tokens(longer)):
        return False
    suffix = " ".join(right_tokens[len(left_tokens):])
    enrichment_terms = {
        "see", "sheet", "page", "pages", "detail", "details", "refer",
        "reference", "provided", "provide", "calculation", "calculations",
        "plan", "plans", "drawing", "drawings", "updated", "included",
    }
    if not enrichment_terms.intersection(suffix.split()):
        return False
    return text_similarity(shorter, longer) >= threshold
