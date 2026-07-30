#!/usr/bin/env python3
"""Local permit comment-response browser with deterministic precedent retrieval."""

from __future__ import annotations

import argparse
import getpass
import html
import json
import math
import mimetypes
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from .source_registry import SourceRegistry
    from .gemini_enrich import GeminiClient, record_digest
    from .rag_search import SearchIndex, normalize_analysis
    from .knowledge_chat import KnowledgeChat
    from .data_trust import searchable_comment, verified_text
    from .comment_dedup import find_duplicate_comments
    from .document_identity import canonicalize_documents, topic_occurrence_key, topic_occurrence_allowed
    from .local_secrets import gemini_api_key
except ImportError:  # Direct `python3 web_app/server.py` execution.
    from source_registry import SourceRegistry
    from gemini_enrich import GeminiClient, record_digest
    from rag_search import SearchIndex, normalize_analysis
    from knowledge_chat import KnowledgeChat
    from data_trust import searchable_comment, verified_text
    from comment_dedup import find_duplicate_comments
    from document_identity import canonicalize_documents, topic_occurrence_key, topic_occurrence_allowed
    from local_secrets import gemini_api_key


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for",
    "from", "has", "have", "in", "is", "it", "of", "on", "or", "that",
    "the", "this", "to", "was", "were", "will", "with", "your", "you",
    "please", "provide", "show", "shall", "per",
}

TECHNICAL_TERMS = {
    "access", "ada", "anchor", "bearing", "beam", "building", "calculation",
    "cbc", "cec", "code", "concrete", "connection", "construction", "cpc",
    "crc", "detail", "dimension", "door", "drain", "egress", "electrical",
    "elevation", "engineering", "fire", "floor", "footing", "foundation",
    "framing", "grading", "guardrail", "hvac", "irrigation", "lateral",
    "load", "mechanical", "plumbing", "rafter", "roof", "seismic", "sewer",
    "shear", "site", "slab", "soil", "stair", "stormwater", "structural",
    "tree", "ventilation", "wall", "window",
}

ADMINISTRATIVE_TERMS = {
    "application", "apply", "contact", "declaration", "email", "fee", "form",
    "invoice", "owner", "payment", "permit", "resubmit", "signature", "stamp",
    "submit", "submittal", "upload",
}

TOPIC_STOP_WORDS = STOP_WORDS | {
    "comment", "comments", "fullset", "general", "markup", "pdf", "plan",
    "plans", "review", "reviewed", "round", "sheet", "sheets",
}


def readable_text(text: str) -> str:
    """Return display-friendly text without changing the immutable source value."""
    value = html.unescape(unicodedata.normalize("NFKC", text or ""))
    value = value.replace("_x000D_", " ").replace("_x000A_", " ")
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(
        r"^(?:response\s*[_:-]\s*)+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value


def normalized_comment(text: str) -> str:
    return readable_text(text).casefold()


def classify_comment(text: str, discipline: str = "") -> str:
    tokens = set(re.findall(r"[a-z]+", readable_text(f"{discipline} {text}").casefold()))
    technical_score = len(tokens & TECHNICAL_TERMS)
    administrative_score = len(tokens & ADMINISTRATIVE_TERMS)
    if administrative_score > technical_score:
        return "nontechnical"
    return "technical"


def topic_tokens(text: str) -> list[str]:
    value = readable_text(text).casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"\b\d+(?:[./'-]\d+)*\b", " number ", value)
    return [
        token for token in re.findall(r"[a-z]+(?:-[a-z]+)?", value)
        if len(token) > 1 and token not in TOPIC_STOP_WORDS
    ]


def topic_label(text: str) -> str:
    value = readable_text(text)
    value = re.sub(r"^comment\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"^markup\s+\S+\.(?:pdf|docx?|xlsx?)(?:\s+\w+\s+review\s+\d+)?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value


def topic_similarity(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    left_counts, right_counts = Counter(left), Counter(right)
    shared = sum(min(left_counts[token], right_counts[token]) for token in left_counts.keys() & right_counts.keys())
    total = sum(max(left_counts[token], right_counts[token]) for token in left_counts.keys() | right_counts.keys())
    return shared / total if total else 0.0


def tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", (text or "").casefold())
        if len(token) > 1 and token not in STOP_WORDS
    ]


def compact_path(path: str) -> str:
    names = [Path(part).name for part in re.split(r"\s+\|\s+", path or "") if part.strip()]
    return " + ".join(names)


class DatasetStore:
    def __init__(
        self,
        dataset_path: Path,
        categories_path: Path,
        source_root: Path,
        source_registry_path: Path | None = None,
        preview_root: Path | None = None,
        enrichment_path: Path | None = None,
        search_index_path: Path | None = None,
        document_authorizer: Any = None,
        gemini_client: GeminiClient | None = None,
        knowledge_gemini_client: GeminiClient | None = None,
        link_reviews_path: Path | None = None,
        workbook_reviews_path: Path | None = None,
    ):
        self.dataset_path = dataset_path.resolve()
        self.categories_path = categories_path.resolve()
        self.source_root = source_root.resolve()
        self.enrichment_path = (enrichment_path or self.categories_path.parent / "gemini_enrichment.json").resolve()
        self.link_reviews_path = (link_reviews_path or self.categories_path.parent / "link_review_decisions.json").resolve()
        self.workbook_reviews_path = (
            workbook_reviews_path
            or self.categories_path.parent / "workbook_review_decisions.json"
        ).resolve()
        self.gemini_client = gemini_client
        self.knowledge_gemini_client = knowledge_gemini_client
        self.search_index = SearchIndex(search_index_path or self.categories_path.parent / "search_index.json")
        self._search_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._dataset_mtime_ns = -1
        self._comments: list[dict[str, Any]] = []
        self._all_comments: list[dict[str, Any]] = []
        self._document_identity: dict[str, Any] = {}
        self._comments_by_id: dict[str, dict[str, Any]] = {}
        self._responses_by_id: dict[str, dict[str, Any]] = {}
        self._links_by_comment: dict[str, dict[str, Any]] = {}
        self._assignments: dict[str, str] = {}
        self._analysis_cache: dict[str, dict[str, Any]] = {}
        self._enrichment_entries: dict[str, dict[str, Any]] = {}
        self._link_review_decisions: dict[str, dict[str, Any]] = {}
        self._workbook_review_decisions: dict[str, dict[str, Any]] = {}
        self.source_registry = SourceRegistry(
            self.dataset_path,
            self.source_root,
            source_registry_path or self.categories_path.parent / "source_registry.json",
            preview_root or self.categories_path.parent / "previews",
            authorizer=document_authorizer,
        )
        self.reload(force=True)
        self._load_categories()
        self._load_enrichment()
        self._load_link_reviews()
        self._load_workbook_reviews()
        self._sync_search_index()
        self.knowledge_chat = KnowledgeChat(self)

    def _load_link_reviews(self) -> None:
        with self._lock:
            if not self.link_reviews_path.is_file():
                self._link_review_decisions = {}
                return
            payload = json.loads(self.link_reviews_path.read_text(encoding="utf-8"))
            decisions = payload.get("decisions", {})
            known_link_ids = {str(row.get("link_id", "")) for row in self._links_by_comment.values()}
            self._link_review_decisions = {
                str(link_id): value for link_id, value in decisions.items()
                if link_id in known_link_ids and isinstance(value, dict)
                and value.get("decision") in {"confirmed", "rejected", "needs_followup"}
            } if isinstance(decisions, dict) else {}

    def _save_link_reviews(self) -> None:
        self.link_reviews_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": "1.0", "decisions": dict(sorted(self._link_review_decisions.items()))}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.link_reviews_path.parent,
            prefix="link-reviews-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.link_reviews_path)

    def _load_workbook_reviews(self) -> None:
        with self._lock:
            if not self.workbook_reviews_path.is_file():
                self._workbook_review_decisions = {}
                return
            payload = json.loads(
                self.workbook_reviews_path.read_text(encoding="utf-8")
            )
            decisions = payload.get("decisions", {})
            self._workbook_review_decisions = {
                str(source): value
                for source, value in decisions.items()
                if isinstance(value, dict)
                and value.get("decision") in {"confirmed", "needs_followup"}
            } if isinstance(decisions, dict) else {}

    def _save_workbook_reviews(self) -> None:
        self.workbook_reviews_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "decisions": dict(sorted(self._workbook_review_decisions.items())),
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.workbook_reviews_path.parent,
            prefix="workbook-reviews-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            json.dump(
                payload, stream, ensure_ascii=False, indent=2, sort_keys=True,
            )
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.workbook_reviews_path)

    def _effective_link_status(self, link: dict[str, Any]) -> str:
        decision = self._link_review_decisions.get(str(link.get("link_id", "")), {})
        return str(decision.get("decision") or link.get("review_status", "not_applicable"))

    def _sync_search_index(self) -> dict[str, int]:
        return self.search_index.sync(
            self._comments,
            lambda row: readable_text(verified_text(row)) if row.get("text_trust_status") == "verified" else (
                self._enrichment_for(str(row["comment_id"]), row).get("display_text") or readable_text(row.get("original_text", ""))
            ),
            lambda comment_id: self._assignments.get(comment_id, "Uncategorized"),
            lambda comment_id: (
                self._effective_link_status(self._links_by_comment.get(comment_id, {})) == "confirmed"
                or self._responses_by_id.get(self._comments_by_id.get(comment_id, {}).get("response_id", ""), {}).get("human_review_status") == "confirmed"
            ),
        )

    def reload(self, force: bool = False) -> None:
        with self._lock:
            stat = self.dataset_path.stat()
            if not force and stat.st_mtime_ns == self._dataset_mtime_ns:
                return
            data = json.loads(self.dataset_path.read_text(encoding="utf-8"))
            comments = data.get("comments", [])
            responses = data.get("responses", [])
            links = data.get("comment_response_links", [])
            comment_ids = [row["comment_id"] for row in comments]
            if len(comment_ids) != len(set(comment_ids)):
                raise ValueError("Dataset contains duplicate comment IDs")
            links_by_comment = {row["comment_id"]: row for row in links}
            self._all_comments = comments
            # Annotate physical files and extracted rows before any runtime
            # search/topic deduplication.  This is deliberately in-memory so
            # opening the app never mutates the production dataset; the
            # canonicalize_documents CLI persists the same registry during
            # ingestion or an explicit repair.
            self._document_identity = canonicalize_documents(comments)
            searchable = [row for row in comments if searchable_comment(row, links_by_comment.get(row["comment_id"]))]
            # Runtime guard: ingestion mistakes must never create repeated list,
            # summary, search-index, or knowledge-chat evidence.
            self._comments, self._duplicate_comments = find_duplicate_comments(searchable, links)
            self._comments_by_id = {row["comment_id"]: row for row in self._comments}
            self._responses_by_id = {row["response_id"]: row for row in responses}
            self._links_by_comment = links_by_comment
            self._dataset_mtime_ns = stat.st_mtime_ns
            self._analysis_cache = {}

    def _load_categories(self) -> None:
        with self._lock:
            if not self.categories_path.is_file():
                self._assignments = {}
                return
            payload = json.loads(self.categories_path.read_text(encoding="utf-8"))
            assignments = payload.get("assignments", {})
            self._assignments = {
                comment_id: str(category)
                for comment_id, category in assignments.items()
                if comment_id in self._comments_by_id and str(category).strip()
            }

    def _load_enrichment(self) -> None:
        with self._lock:
            if not self.enrichment_path.is_file():
                self._enrichment_entries = {}
                return
            payload = json.loads(self.enrichment_path.read_text(encoding="utf-8"))
            entries = payload.get("entries", {})
            self._enrichment_entries = entries if isinstance(entries, dict) else {}

    def _enrichment_for(self, record_id: str, record: dict[str, Any]) -> dict[str, Any]:
        entry = self._enrichment_entries.get(record_id, {})
        if entry.get("input_sha256") != record_digest(record):
            return {}
        return entry

    def _save_categories(self) -> None:
        self.categories_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "assignments": dict(sorted(self._assignments.items())),
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.categories_path.parent,
            prefix="categories-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.categories_path)

    def set_category(self, comment_ids: list[str], category: str) -> dict[str, Any]:
        category = re.sub(r"\s+", " ", category).strip()
        if len(category) > 80:
            raise ValueError("Category must be 80 characters or fewer")
        if not comment_ids or len(comment_ids) > 500:
            raise ValueError("Choose between 1 and 500 comments")
        unknown = [comment_id for comment_id in comment_ids if comment_id not in self._comments_by_id]
        if unknown:
            raise ValueError(f"Unknown comment ID: {unknown[0]}")
        with self._lock:
            for comment_id in comment_ids:
                if category:
                    self._assignments[comment_id] = category
                else:
                    self._assignments.pop(comment_id, None)
            self._save_categories()
            self._sync_search_index()
            self._search_cache.clear()
        return {"updated": len(comment_ids), "category": category}

    def cities(self) -> list[dict[str, Any]]:
        counts = Counter(row["city"] for row in self._comments)
        return [{"name": city, "count": counts[city]} for city in sorted(counts)]

    def categories(self, city: str = "") -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        for comment in self._comments:
            if city and comment["city"] != city:
                continue
            value = self._assignments.get(comment["comment_id"], "Uncategorized")
            counts[value] += 1
        return [{"name": name, "count": counts[name]} for name in sorted(counts)]

    def _view_comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        comment_id = comment["comment_id"]
        response_id = comment.get("response_id", "")
        response = self._responses_by_id.get(response_id)
        link = self._links_by_comment.get(comment_id, {})
        comment_enrichment = self._enrichment_for(comment_id, comment)
        response_enrichment = self._enrichment_for(response_id, response) if response else {}
        comment_text = verified_text(comment)
        response_text = verified_text(response) if response else ""
        comment_sources = self._source_references(comment_id, comment_text)
        response_sources = (
            self._source_references(response["response_id"], response_text)
            if response else []
        )
        primary_comment_source = next((
            source for source in comment_sources
            if source.get("relation") == "Primary source"
        ), None)
        primary_response_source = next((
            source for source in response_sources
            if source.get("relation") == "Primary source"
        ), None)
        event_sources = {
            str(
                ((source.get("location") or {}).get("metadata") or {}).get(
                    "issue_event_id", ""
                )
            ): source
            for source in comment_sources
            if isinstance(source.get("location"), dict)
        }
        discussion_events = [
            event for event in comment.get("issue_thread_events", []) or []
            if isinstance(event, dict) and str(
                event.get("exact_text", "")
            ).strip()
        ]
        discussion_events.sort(key=lambda event: (
            0 if event.get("occurred_at") else 1,
            str(event.get("occurred_at", "")),
            int(event.get("source_order") or 0),
        ))
        timeline_events: list[dict[str, Any]] = [{
            "event_id": f"{comment_id}-government-comment",
            "event_type": "government_comment",
            "actor_role": "government",
            "actor": str(comment.get("reviewer", "")),
            "occurred_at": "",
            "occurred_at_label": "",
            "label": "Government comment",
            "text": readable_text(comment_text),
            "review_round": str(comment.get("review_round", "")),
            "source": primary_comment_source,
        }]
        for event in discussion_events:
            event_type = str(event.get("event_type", "discussion_note"))
            timeline_events.append({
                "event_id": str(event.get("event_id", "")),
                "event_type": event_type,
                "actor_role": str(event.get("actor_role", "unknown")),
                "actor": str(event.get("actor", "")),
                "occurred_at": str(event.get("occurred_at", "")),
                "occurred_at_label": str(
                    event.get("occurred_at_label", "")
                ),
                "label": (
                    "Reviewer follow-up"
                    if event_type == "reviewer_follow_up"
                    else "Applicant response"
                    if event_type == "applicant_response"
                    else "Discussion note"
                ),
                "text": readable_text(event.get("exact_text", "")),
                "review_round": str(
                    event.get("review_round")
                    or comment.get("review_round", "")
                ),
                "source": event_sources.get(
                    str(event.get("event_id", ""))
                ),
            })
        if response:
            timeline_events.append({
                "event_id": f"{response_id}-current-response",
                "event_type": "current_applicant_response",
                "actor_role": "company",
                "actor": "",
                "occurred_at": "",
                "occurred_at_label": "",
                "label": (
                    "Current applicant response"
                    if discussion_events else "Company response"
                ),
                "text": readable_text(response_text),
                "review_round": str(
                    response.get("response_letter_round")
                    or comment.get("review_round", "")
                ),
                "source": primary_response_source,
            })
        return {
            "comment_id": comment_id,
            "source_file_id": comment.get("source_file_id", ""),
            "canonical_document_id": comment.get("canonical_document_id", ""),
            "canonical_comment_id": comment.get("canonical_comment_id", ""),
            "occurrence_type": comment.get("occurrence_type", "newly_issued"),
            "city": comment.get("city", "unknown"),
            "property_project": comment.get("property_project", "unknown"),
            "review_round": comment.get("review_round", "unknown"),
            "discipline": comment.get("discipline", "unknown"),
            "comment_type": classify_comment(comment_text, comment.get("discipline", "")),
            "reviewer": comment.get("reviewer", ""),
            "comment_number": comment.get("comment_number", ""),
            "original_text": comment_text,
            "display_text": readable_text(comment_text) if comment.get("text_trust_status") == "verified" else (comment_enrichment.get("display_text") or readable_text(comment_text)),
            "display_blocks": comment_enrichment.get("blocks", []),
            "source_filename": compact_path(comment.get("source_document", "")),
            "sources": comment_sources,
            "source_location": comment.get("source_location", "unknown"),
            "extraction_method": comment.get("extraction_method", ""),
            "extraction_confidence": comment.get("extraction_confidence", ""),
            "match_status": comment.get("match_status", "unmatched"),
            "human_review_status": comment.get("human_review_status", "pending"),
            "category": self._assignments.get(comment_id, "Uncategorized"),
            "response": ({
                "response_id": response["response_id"],
                "original_text": response_text,
                "display_text": readable_text(response_text) if response.get("text_trust_status") == "verified" else (response_enrichment.get("display_text") or readable_text(response_text)),
                "display_blocks": response_enrichment.get("blocks", []),
                "source_filename": compact_path(response.get("source_document", "")),
                "sources": response_sources,
                "source_location": response.get("source_location", "unknown"),
                "human_review_status": response.get("human_review_status", "pending"),
            } if response else None),
            "link": {
                "link_id": link.get("link_id", ""),
                "match_confidence": link.get("match_confidence", ""),
                "matching_method": link.get("matching_method", ""),
                "review_status": self._effective_link_status(link),
            },
            "issue_thread": {
                "thread_id": str(
                    comment.get("issue_thread_id", comment_id)
                ),
                "grouping_status": str(
                    comment.get("issue_grouping_status", "single_record")
                ),
                "grouping_method": str(
                    comment.get(
                        "issue_grouping_method", "single_source_record",
                    )
                ),
                "status": str(comment.get("issue_status", "")),
                "event_count": len(timeline_events),
                "events": timeline_events,
            },
        }

    def link_review_queue(self, status: str = "pending", city: str = "", summary_only: bool = False) -> dict[str, Any]:
        self.reload()
        allowed_statuses = {"pending", "suggested", "confirmed", "rejected", "needs_review", "needs_followup", "all"}
        if status not in allowed_statuses:
            raise ValueError("Unknown link-review status")
        eligible: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for comment in self._comments:
            link = self._links_by_comment.get(str(comment.get("comment_id", "")), {})
            if not link.get("response_id"):
                continue
            base_status = str(link.get("review_status", ""))
            link_id = str(link.get("link_id", ""))
            if base_status not in {"suggested", "needs_review"} and link_id not in self._link_review_decisions:
                continue
            effective = self._effective_link_status(link)
            eligible.append((comment, link, effective))

        counts = Counter(effective for _, _, effective in eligible)
        count_payload = {
            "total": len(eligible), "suggested": counts["suggested"],
            "confirmed": counts["confirmed"], "rejected": counts["rejected"],
            "needs_review": counts["needs_review"], "needs_followup": counts["needs_followup"],
            "completed": counts["confirmed"] + counts["rejected"],
        }
        if summary_only:
            return {"items": [], "counts": count_payload}
        items: list[dict[str, Any]] = []
        for comment, link, effective in eligible:
            if city and comment.get("city") != city:
                continue
            if status == "pending" and effective not in {"suggested", "needs_review", "needs_followup"}:
                continue
            if status not in {"pending", "all"} and effective != status:
                continue
            view = self._view_comment(comment)
            decision = self._link_review_decisions.get(str(link.get("link_id", "")), {})
            items.append({
                "link_id": link.get("link_id", ""), "status": effective,
                "base_status": link.get("review_status", ""),
                "note": decision.get("note", ""), "updated_at": decision.get("updated_at"),
                "comment": view,
            })
        items.sort(key=lambda item: (
            str(item["comment"].get("city", "")), str(item["comment"].get("property_project", "")),
            str(item["comment"].get("review_round", "")), str(item["comment"].get("discipline", "")),
            str(item["comment"].get("comment_number", "")), str(item.get("link_id", "")),
        ))
        return {
            "items": items,
            "counts": count_payload,
        }

    def set_link_review(self, link_id: str, decision: str, note: str = "") -> dict[str, Any]:
        decision = decision.strip().casefold()
        note = re.sub(r"\s+", " ", note).strip()
        if decision not in {"", "confirmed", "rejected", "needs_followup"}:
            raise ValueError("Decision must be confirmed, rejected, needs_followup, or empty")
        if len(note) > 500:
            raise ValueError("Review note must be 500 characters or fewer")
        link = next((row for row in self._links_by_comment.values() if str(row.get("link_id", "")) == link_id), None)
        if not link or not link.get("response_id"):
            raise ValueError("Unknown response link")
        with self._lock:
            if decision:
                self._link_review_decisions[link_id] = {
                    "decision": decision, "note": note, "updated_at": int(time.time()),
                }
            else:
                self._link_review_decisions.pop(link_id, None)
            self._save_link_reviews()
            self._search_cache.clear()
            self._sync_search_index()
        return {"link_id": link_id, "decision": decision or str(link.get("review_status", "suggested"))}

    def _structured_workbook_groups(
        self,
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for comment in self._all_comments:
            if (
                comment.get("extraction_method")
                != "local_structured_spreadsheet"
            ):
                continue
            link = self._links_by_comment.get(
                str(comment.get("comment_id", "")), {},
            )
            if (
                link.get("provenance")
                != "local_structured_gemini_verified"
            ):
                continue
            source = str(comment.get("source_document", "")).strip()
            if (
                not source
                or Path(source).suffix.casefold() not in {".xlsx", ".csv"}
            ):
                continue
            groups.setdefault(source, []).append(comment)
        return groups

    def _workbook_completeness(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        artifact_ids = {
            str(
                (row.get("ingestion_audit") or {}).get("artifact_id", "")
            )
            for row in rows
            if isinstance(row.get("ingestion_audit"), dict)
        }
        if len(artifact_ids) != 1 or not next(iter(artifact_ids), ""):
            return {
                "can_confirm": False,
                "reason": "Rows do not share one ingestion artifact",
            }
        artifact_id = next(iter(artifact_ids))
        manifest_path = (
            self.dataset_path.parent
            / "ingestion_artifacts"
            / artifact_id
            / "completeness_manifest.json"
        )
        if not manifest_path.is_file():
            return {
                "can_confirm": False,
                "reason": "Structured completeness manifest is missing",
            }
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "can_confirm": False,
                "reason": "Structured completeness manifest is unreadable",
            }
        expected = int(manifest.get("candidate_comment_count") or 0)
        unresolved = int(manifest.get("unresolved_signal_count") or 0)
        can_confirm = (
            manifest.get("completion_status") == "complete"
            and manifest.get("requires_visual") is not True
            and unresolved == 0
            and expected == len(rows)
            and not manifest.get("duplicate_unit_ids")
            and not manifest.get("unassigned_unit_ids")
        )
        return {
            "can_confirm": can_confirm,
            "reason": (
                ""
                if can_confirm
                else "Local completeness checks did not pass"
            ),
            "artifact_id": artifact_id,
            "expected_comments": expected,
            "unresolved_signals": unresolved,
            "requires_visual": bool(manifest.get("requires_visual")),
        }

    def workbook_review_queue(
        self,
        status: str = "pending",
        city: str = "",
        summary_only: bool = False,
    ) -> dict[str, Any]:
        self.reload()
        if status not in {
            "pending", "confirmed", "needs_followup", "all",
        }:
            raise ValueError("Unknown workbook-review status")
        items: list[dict[str, Any]] = []
        for source, rows in self._structured_workbook_groups().items():
            rows.sort(key=lambda row: (
                int(row.get("source_row") or 0),
                str(row.get("comment_id", "")),
            ))
            if city and rows[0].get("city") != city:
                continue
            links = [
                self._links_by_comment.get(
                    str(row.get("comment_id", "")), {},
                )
                for row in rows
            ]
            dataset_confirmed = all(
                row.get("search_eligible") is True
                and row.get("text_trust_status") == "verified"
                for row in rows
            ) and all(
                link.get("review_status") in {
                    "confirmed", "not_required",
                }
                for link in links
            )
            decision = self._workbook_review_decisions.get(source, {})
            effective = (
                "confirmed"
                if dataset_confirmed
                else "needs_followup"
                if decision.get("decision") == "needs_followup"
                else "pending"
            )
            completeness = self._workbook_completeness(rows)
            row_views = [self._view_comment(row) for row in rows]
            first_source = next((
                reference
                for view in row_views
                for reference in view.get("sources", [])
                if reference.get("kind") == "local"
            ), None)
            comment_columns = sorted({
                re.sub(
                    r"\d.*$", "",
                    str(row.get("source_cell_range", "")),
                )
                for row in rows
                if str(row.get("source_cell_range", ""))
            })
            response_columns = sorted({
                re.sub(
                    r"\d.*$", "",
                    str(
                        self._responses_by_id.get(
                            str(row.get("response_id", "")), {},
                        ).get("source_cell_range", "")
                    ),
                )
                for row in rows
                if str(row.get("response_id", ""))
            } - {""})
            items.append({
                "source_document": source,
                "filename": Path(source).name,
                "status": effective,
                "note": str(decision.get("note", "")),
                "updated_at": decision.get("updated_at"),
                "city": str(rows[0].get("city", "")),
                "property_project": str(
                    rows[0].get("property_project", "")
                ),
                "review_rounds": sorted({
                    str(row.get("review_round", "")) for row in rows
                }),
                "comment_count": len(rows),
                "response_count": sum(
                    bool(row.get("response_id")) for row in rows
                ),
                "comment_columns": comment_columns,
                "response_columns": response_columns,
                "source": first_source,
                "rows": row_views if not summary_only else [],
                "structural_checks": completeness,
            })
        items.sort(key=lambda item: (
            item["city"], item["property_project"], item["filename"],
        ))
        counts = Counter(item["status"] for item in items)
        total = len(items)
        visible_items = (
            items
            if status == "all"
            else [item for item in items if item["status"] == status]
        )
        return {
            "items": [] if summary_only else visible_items,
            "counts": {
                "total": total,
                "pending": counts["pending"],
                "confirmed": counts["confirmed"],
                "needs_followup": counts["needs_followup"],
            },
        }

    def set_workbook_review(
        self,
        source_document: str,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        source_document = source_document.strip()
        decision = decision.strip().casefold()
        note = re.sub(r"\s+", " ", note).strip()
        if decision not in {"confirmed", "needs_followup"}:
            raise ValueError(
                "Decision must be confirmed or needs_followup"
            )
        if len(note) > 500:
            raise ValueError(
                "Workbook review note must be 500 characters or fewer"
            )
        groups = self._structured_workbook_groups()
        rows = groups.get(source_document)
        if not rows:
            raise ValueError("Unknown structured workbook")
        completeness = self._workbook_completeness(rows)
        if decision == "confirmed" and not completeness["can_confirm"]:
            raise ValueError(
                str(completeness.get("reason"))
                or "Workbook did not pass local completeness checks"
            )
        now = int(time.time())
        with self._lock:
            data = json.loads(
                self.dataset_path.read_text(encoding="utf-8")
            )
            target_ids = {
                str(row.get("comment_id", "")) for row in rows
            }
            if decision == "confirmed":
                comments_by_id = {
                    str(row.get("comment_id", "")): row
                    for row in data.get("comments", [])
                }
                responses_by_id = {
                    str(row.get("response_id", "")): row
                    for row in data.get("responses", [])
                }
                links_by_comment = {
                    str(row.get("comment_id", "")): row
                    for row in data.get("comment_response_links", [])
                }
                if not target_ids.issubset(comments_by_id):
                    raise RuntimeError(
                        "Dataset changed during workbook review; reload and retry"
                    )
                for comment_id in target_ids:
                    comment = comments_by_id[comment_id]
                    link = links_by_comment.get(comment_id, {})
                    if (
                        comment.get("source_document") != source_document
                        or comment.get("extraction_method")
                        != "local_structured_spreadsheet"
                        or link.get("provenance")
                        != "local_structured_gemini_verified"
                    ):
                        raise RuntimeError(
                            "Workbook rows changed during review; reload and retry"
                        )
                    comment.update({
                        "verified_text": str(
                            comment.get("original_text", "")
                        ),
                        "source_status": "confirmed",
                        "human_review_status": "confirmed",
                        "verification_status": "confirmed",
                        "text_trust_status": "verified",
                        "search_eligible": True,
                        "extraction_confidence": 1.0,
                    })
                    audit_payload = comment.setdefault(
                        "ingestion_audit", {},
                    )
                    if isinstance(audit_payload, dict):
                        audit_payload["human_workbook_verification"] = {
                            "decision": "confirmed",
                            "note": note,
                            "updated_at": now,
                            "artifact_id": completeness.get(
                                "artifact_id", ""
                            ),
                        }
                    response_id = str(comment.get("response_id", ""))
                    response = responses_by_id.get(response_id)
                    if response:
                        response.update({
                            "verified_text": str(
                                response.get("original_text", "")
                            ),
                            "human_review_status": "confirmed",
                            "verification_status": "confirmed",
                            "text_trust_status": "verified",
                            "search_eligible": True,
                            "extraction_confidence": 1.0,
                        })
                        response_audit = response.setdefault(
                            "ingestion_audit", {},
                        )
                        if isinstance(response_audit, dict):
                            response_audit[
                                "human_workbook_verification"
                            ] = {
                                "decision": "confirmed",
                                "note": note,
                                "updated_at": now,
                            }
                    link.update({
                        "review_status": (
                            "confirmed" if response else "not_required"
                        ),
                        "verification_status": "confirmed",
                        "match_confidence": 1.0 if response else 0.0,
                    })
                    link_audit = link.setdefault("ingestion_audit", {})
                    if isinstance(link_audit, dict):
                        link_audit["human_workbook_verification"] = {
                            "decision": "confirmed",
                            "note": note,
                            "updated_at": now,
                        }
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.dataset_path.parent,
                    prefix="dataset-workbook-review-",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    json.dump(
                        data,
                        stream,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                    temporary = Path(stream.name)
                os.replace(temporary, self.dataset_path)
            self._workbook_review_decisions[source_document] = {
                "decision": decision,
                "note": note,
                "updated_at": now,
                "comment_count": len(rows),
                "artifact_id": completeness.get("artifact_id", ""),
            }
            self._save_workbook_reviews()
            self.reload(force=True)
            self._load_link_reviews()
            self._sync_search_index()
            self._search_cache.clear()
        return {
            "source_document": source_document,
            "decision": decision,
            "updated": len(rows) if decision == "confirmed" else 0,
        }

    def _source_references(self, owner_id: str, text: str) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in self.source_registry.sources_for_owner(owner_id):
            source["kind"] = "local"
            source["filename"] = source["document"]["filename"]
            references.append(source)
            seen.add(f"local:{source['source_id']}")
        display = readable_text(text)
        for url in re.findall(r"https?://[^\s<>\"]+", display):
            clean_url = url.rstrip(".,);]")
            key = f"url:{clean_url}"
            if key in seen:
                continue
            seen.add(key)
            references.append({
                "kind": "external",
                "url": clean_url,
                "filename": "Referenced web resource",
                "location": "",
                "relation": "Referenced in text",
            })
        return references

    def _common_topics(self, comments: list[dict[str, Any]], limit: int = 6) -> tuple[int, list[dict[str, Any]]]:
        # A topic occurrence is one logical comment in one canonical document,
        # never one physical filename or one extraction row.  Legacy rows that
        # have not been canonicalized use their comment id as a safe fallback.
        occurrence_rows: list[dict[str, Any]] = []
        seen_occurrences: set[tuple[str, str]] = set()
        for row in comments:
            if not topic_occurrence_allowed(row):
                continue
            key = topic_occurrence_key(row)
            if key in seen_occurrences:
                continue
            seen_occurrences.add(key)
            occurrence_rows.append(row)

        count = len(occurrence_rows)
        parents = list(range(count))
        tokenized = [topic_tokens(verified_text(row)) for row in occurrence_rows]
        signatures = [" ".join(tokens) for tokens in tokenized]

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        for left in range(count):
            if not tokenized[left]:
                continue
            for right in range(left + 1, count):
                if signatures[left] == signatures[right]:
                    union(left, right)
                    continue
                shorter = min(len(tokenized[left]), len(tokenized[right]))
                threshold = 0.8 if shorter <= 5 else 0.7
                if topic_similarity(tokenized[left], tokenized[right]) >= threshold:
                    union(left, right)

        groups: dict[int, list[int]] = {}
        for index in range(count):
            groups.setdefault(find(index), []).append(index)

        common: list[dict[str, Any]] = []
        for indexes in groups.values():
            if len(indexes) < 2:
                continue
            representative_index = max(indexes, key=lambda item: (len(tokenized[item]), -item))
            representative = occurrence_rows[representative_index]
            document_ids = {
                str(occurrence_rows[item].get("canonical_document_id") or topic_occurrence_key(occurrence_rows[item])[0])
                for item in indexes
            }
            # A topic is common only across independent logical documents.  A
            # repeated row or a renamed/re-exported source is one occurrence.
            if len(document_ids) < 2:
                continue
            document_registry = getattr(self, "_document_identity", {}).get("canonical_documents", {}) if self is not None else {}
            duplicate_files_excluded = sum(
                max(0, int(document_registry.get(document_id, {}).get("duplicate_group_size", 1)) - 1)
                for document_id in document_ids
            )
            common.append({
                "label": topic_label(verified_text(representative)),
                "occurrences": len(indexes),
                "independent_source_documents": len(document_ids),
                "physical_duplicate_files_excluded": duplicate_files_excluded,
                "projects": len({occurrence_rows[item].get("property_project", "") for item in indexes}),
                "rounds": len({(occurrence_rows[item].get("property_project", ""), occurrence_rows[item].get("review_round", "")) for item in indexes}),
                "cities": len({occurrence_rows[item].get("city", "") for item in indexes}),
                "comment_ids": [occurrence_rows[item]["comment_id"] for item in indexes],
            })
        common.sort(key=lambda row: (-row["occurrences"], row["label"].casefold()))
        return len(groups), common[:limit]

    def analysis(self, city: str) -> dict[str, Any]:
        if city in self._analysis_cache:
            return self._analysis_cache[city]
        comments = [row for row in self._comments if row.get("city") == city]
        technical = sum(classify_comment(verified_text(row), row.get("discipline", "")) == "technical" for row in comments)
        unique_comments = len({normalized_comment(verified_text(row)) for row in comments})
        topic_count, common_topics = self._common_topics(comments)
        projects = len({row.get("property_project", "") for row in comments})
        rounds = len({(row.get("property_project", ""), row.get("review_round", "")) for row in comments})
        nontechnical = len(comments) - technical
        summary = (
            f"{city} has {len(comments)} historical review comments across {projects} project scopes "
            f"and {rounds} review cycles. {technical} are classified as technical and {nontechnical} "
            f"as administrative or non-technical. After line-break normalization, {unique_comments} "
            f"comment texts are distinct; topic grouping identifies {topic_count} recurring or standalone issues."
        )
        payload = {
            "summary": summary,
            "total_comments": len(comments),
            "unique_comments": unique_comments,
            "topic_count": topic_count,
            "common_topic_count": len(common_topics),
            "technical": technical,
            "nontechnical": nontechnical,
            "projects": projects,
            "review_cycles": rounds,
            "common_topics": common_topics,
            "method_note": "A common topic requires the same normalized topic in at least two independent canonical source documents. Renamed, re-exported, archived, and copied files count once; repeated rows inside one document are excluded.",
        }
        self._analysis_cache[city] = payload
        return payload

    def data(self, city: str = "") -> dict[str, Any]:
        self.reload()
        with self._lock:
            comments = [
                self._view_comment(row) for row in self._comments
                if not city or row["city"] == city
            ]
            matched = sum(row["match_status"] == "matched" for row in comments)
            return {
                "cities": self.cities(),
                "categories": self.categories(city),
                "comments": comments,
                "stats": {
                    "comments": len(comments),
                    "matched": matched,
                    "unmatched": len(comments) - matched,
                },
                "analysis": self.analysis(city) if city else None,
            }

    def search(self, city: str, query: str, limit: int = 30) -> list[dict[str, Any]]:
        self.reload()
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        candidates = [row for row in self._comments if not city or row["city"] == city]
        if not candidates:
            return []
        tokenized = [tokenize(verified_text(row)) for row in candidates]
        document_frequency: Counter[str] = Counter()
        for tokens in tokenized:
            document_frequency.update(set(tokens))
        count = len(candidates)
        idf = {
            token: math.log((count + 1) / (document_frequency[token] + 0.5)) + 1
            for token in set(query_tokens)
        }
        query_counts = Counter(query_tokens)
        query_vector = {token: frequency * idf[token] for token, frequency in query_counts.items()}
        query_norm = math.sqrt(sum(value * value for value in query_vector.values())) or 1.0
        results: list[dict[str, Any]] = []
        query_phrase = re.sub(r"\s+", " ", query.casefold()).strip()
        for comment, tokens in zip(candidates, tokenized):
            counts = Counter(token for token in tokens if token in idf)
            if not counts:
                continue
            vector = {token: frequency * idf[token] for token, frequency in counts.items()}
            norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
            score = sum(query_vector.get(token, 0) * value for token, value in vector.items()) / (query_norm * norm)
            text_lower = verified_text(comment).casefold()
            if query_phrase and query_phrase in text_lower:
                score += 0.35
            results.append({"comment_id": comment["comment_id"], "score": round(score, 4)})
        results.sort(key=lambda row: (-row["score"], row["comment_id"]))
        return results[: max(1, min(limit, 100))]

    def gemini_search(
        self, city: str, query: str, limit: int = 10,
        discipline: str = "", category: str = "",
    ) -> dict[str, Any]:
        self.reload()
        self._sync_search_index()
        final_limit = max(1, min(limit, 10))
        cache_key = json.dumps(["accuracy-rag-2.0", getattr(self.gemini_client, "model", ""), city, discipline, category, query.casefold().strip(), final_limit], ensure_ascii=False)
        cached = self._search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return {**cached[1], "cached": True}

        timings: dict[str, int] = {}
        failures: list[str] = []
        started = time.monotonic()
        analysis = normalize_analysis({}, query)
        if self.gemini_client:
            try:
                analysis = normalize_analysis(self.gemini_client.analyze_search_query(query), query)
            except RuntimeError:
                failures.append("query_analysis")
        timings["query_analysis_ms"] = round((time.monotonic() - started) * 1000)
        # Explicit UI filters are authoritative; inferred values only help ranking.
        if city:
            analysis["city"] = city
        if discipline:
            analysis["discipline"] = discipline
        if category:
            analysis["category"] = category

        rewrite_started = time.monotonic()
        rewrites: list[str] = []
        if self.gemini_client and "query_analysis" not in failures:
            try:
                rewrites = self.gemini_client.rewrite_search_query(query, analysis)
            except RuntimeError:
                failures.append("query_rewrites")
        timings["query_rewrites_ms"] = round((time.monotonic() - rewrite_started) * 1000)

        retrieval_started = time.monotonic()
        has_embeddings = any(
            unit.get("embedding")
            for record in self.search_index.records.values()
            for unit in record.get("search_units", [])
        )
        merged: dict[str, dict[str, Any]] = {}
        for search_query in [query, *rewrites]:
            query_embedding: list[float] | None = None
            if self.gemini_client and has_embeddings:
                try:
                    query_embedding = self.gemini_client.embed_query(search_query)
                except RuntimeError:
                    if "query_embedding" not in failures:
                        failures.append("query_embedding")
            rows = self.search_index.retrieve(
                search_query, analysis, city, query_embedding, discipline, category,
                vector_limit=100, keyword_limit=100, candidate_limit=200,
            )
            for row in rows:
                existing = merged.get(row["comment_id"])
                if not existing or row["score"] > existing["score"]:
                    row["retrieval_queries"] = [search_query]
                    merged[row["comment_id"]] = row
                elif search_query not in existing["retrieval_queries"]:
                    existing["retrieval_queries"].append(search_query)
        candidates = sorted(merged.values(), key=lambda row: (-row["score"], row["comment_id"]))[:200]
        timings["retrieval_ms"] = round((time.monotonic() - retrieval_started) * 1000)
        compact_candidates = [{
            "candidate_id": row["comment_id"],
            "city": row["record"].get("city", ""),
            "discipline": row["record"].get("discipline", ""),
            "category": row["record"].get("category", ""),
            "comment_excerpt": row.get("matched_excerpt", "")[:1200],
            "code_sections": row["record"].get("code_sections", []),
            "accepted": bool(row["record"].get("accepted")),
            "hybrid_score": row["score"],
            "data_quality_flags": row["record"].get("data_quality_flags", []),
        } for row in candidates]

        evaluation_started = time.monotonic()
        evaluations: list[dict[str, Any]] = []
        if self.gemini_client and compact_candidates and not failures:
            for offset in range(0, len(compact_candidates), 25):
                try:
                    evaluations.extend(self.gemini_client.evaluate_search_candidates(analysis, compact_candidates[offset:offset + 25]))
                except RuntimeError:
                    failures.append("candidate_evaluation")
                    evaluations = []
                    break
        timings["candidate_evaluation_ms"] = round((time.monotonic() - evaluation_started) * 1000)

        evaluation_by_id = {str(item.get("candidate_id", "")): item for item in evaluations}
        strongest = [
            row for row in candidates
            if evaluation_by_id.get(row["comment_id"], {}).get("match_class") in {"direct", "related", "uncertain"}
        ]
        strongest.sort(key=lambda row: -float(evaluation_by_id[row["comment_id"]].get("relevance_score", 0)))
        strongest = strongest[:30]

        deep_started = time.monotonic()
        deep_results: list[dict[str, Any]] = []
        full_candidates: list[dict[str, Any]] = []
        if self.gemini_client and strongest and not failures:
            for row in strongest:
                comment = self._comments_by_id[row["comment_id"]]
                response = self._responses_by_id.get(comment.get("response_id", ""))
                link = self._links_by_comment.get(row["comment_id"], {})
                full_candidates.append({
                    "candidate_id": row["comment_id"],
                    "city": comment.get("city", ""), "discipline": comment.get("discipline", ""),
                    "property_project": comment.get("property_project", ""), "review_round": comment.get("review_round", ""),
                    "heading_and_original_comment": verified_text(comment),
                    "matched_search_unit": row.get("matched_excerpt", ""),
                    "historical_response": verified_text(response) if response else "",
                    "response_review_status": response.get("human_review_status", "") if response else "no_response",
                    "response_link_review_status": self._effective_link_status(link),
                    "data_quality_flags": row["record"].get("data_quality_flags", []),
                    "initial_evaluation": evaluation_by_id[row["comment_id"]],
                })
            try:
                deep_results = self.gemini_client.deep_rerank(analysis, full_candidates)
            except RuntimeError:
                failures.append("deep_reranking")
        timings["deep_reranking_ms"] = round((time.monotonic() - deep_started) * 1000)

        verification_started = time.monotonic()
        verified: list[dict[str, Any]] = []
        verification_completed = False
        if self.gemini_client and not failures:
            full_by_id = {item["candidate_id"]: item for item in full_candidates}
            proposed = [{**item, "stored_record": full_by_id.get(str(item.get("candidate_id", "")), {})} for item in deep_results if item.get("match_class") in {"direct", "related"}][:15]
            try:
                verified = self.gemini_client.verify_search_results(analysis, proposed)
                verification_completed = True
            except RuntimeError:
                failures.append("verification")
        timings["verification_ms"] = round((time.monotonic() - verification_started) * 1000)

        results: list[dict[str, Any]] = []
        engine_label = "Hybrid database fallback"
        if verification_completed and not failures:
            for item in verified[:final_limit]:
                results.append({
                    "comment_id": str(item.get("candidate_id", "")),
                    "score": round(max(0.0, min(1.0, float(item.get("relevance_score", 0)))), 4),
                    "match_class": item.get("match_class"),
                    "confidence": round(max(0.0, min(1.0, float(item.get("confidence", 0)))), 4),
                    "response_applicable": bool(item.get("response_applicable")),
                    "important_difference": "; ".join(str(value) for value in item.get("important_differences", []) if str(value).strip()),
                    "reason": str(item.get("reason", "")).strip(),
                })
            engine_label = "Gemini accuracy-verified RAG"
        elif not self.gemini_client or failures:
            # Never label deterministic candidates as verified or direct.
            fallback_candidates = [
                row for row in candidates
                if row.get("keyword_score", 0) >= 0.22 or row.get("semantic_score", 0) >= (0.70 if has_embeddings else 0.34)
            ][:final_limit]
            results = [{
                "comment_id": row["comment_id"],
                "score": row["score"],
                "match_class": "unverified",
                "confidence": 0.0,
                "response_applicable": False,
                "important_difference": "Semantic verification is unavailable; this result is not classified as a direct precedent.",
                "reason": "Unverified deterministic candidate from lexical, vector, code, and metadata retrieval.",
            } for row in fallback_candidates]
        timings["total_ms"] = round((time.monotonic() - started) * 1000)
        payload = {
            "results": results,
            "engine_label": engine_label,
            "candidate_count": len(candidates),
            "has_direct_matches": any(row.get("match_class") == "direct" for row in results),
            "no_result_message": "" if results else "No sufficiently relevant historical precedent was found.",
            "timings": timings,
            "gemini_failures": failures,
            "cached": False,
        }
        if os.environ.get("PERMIT_SEARCH_DEBUG") == "1":
            payload["diagnostics"] = {
                "pipeline_version": "accuracy-rag-2.0", "prompt_version": "search-2.0",
                "gemini_model": getattr(self.gemini_client, "model", "") if self.gemini_client else "",
                "parsed_query": analysis, "query_rewrites": rewrites,
                "retrieval": [{"comment_id": row["comment_id"], "queries": row.get("retrieval_queries", []), "score": row["score"], "unit_id": row.get("matched_unit_id", "")} for row in candidates],
                "candidate_evaluations": evaluations, "deep_ranking": deep_results,
                "verification": verified,
                "final_source_ids": {row["comment_id"]: [source.get("source_id") for source in self.source_registry.sources_for_owner(row["comment_id"])] for row in results},
            }
        self._search_cache[cache_key] = (time.monotonic(), payload)
        return payload

class PermitHandler(BaseHTTPRequestHandler):
    server_version = "PermitBrowser/1.0"

    @property
    def app(self) -> "PermitServer":
        return self.server  # type: ignore[return-value]

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect_root_to_localhost(self, path: str) -> bool:
        host = self.headers.get("Host", "")
        hostname, separator, port = host.partition(":")
        if path not in {"", "/", "/index.html"} or hostname not in {"127.0.0.1", "0.0.0.0", "::1"}:
            return False
        authority = f"localhost:{port}" if separator and port else "localhost"
        self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
        self.send_header("Location", f"http://{authority}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > 1_000_000:
            raise ValueError("Request body must be between 1 byte and 1 MB")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _registry_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        elif isinstance(exc, KeyError):
            self._error(HTTPStatus.NOT_FOUND, str(exc.args[0] if exc.args else exc))
        elif isinstance(exc, FileNotFoundError):
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        elif isinstance(exc, (ValueError, RuntimeError, TypeError)):
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc))
        else:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to open source")

    def _serve_document(self, document_id: str, mode: str, send_body: bool = True) -> None:
        try:
            delivery = self.app.store.source_registry.delivery(
                document_id, mode, self.headers.get("Range", ""),
            )
        except (PermissionError, KeyError, FileNotFoundError, ValueError, RuntimeError) as exc:
            self._registry_error(exc)
            return
        path = delivery["path"]
        length = delivery["end"] - delivery["start"] + 1
        filename = str(delivery["filename"]).replace('"', "")
        self.send_response(delivery["status"])
        self.send_header("Content-Type", delivery["mime_type"])
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'{delivery["disposition"]}; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        if delivery["mime_type"] == "application/pdf":
            self.send_header("Accept-Ranges", "bytes")
        if delivery["status"] == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f'bytes {delivery["start"]}-{delivery["end"]}/{delivery["size"]}')
        self.end_headers()
        if not send_body:
            return
        with path.open("rb") as stream:
            stream.seek(delivery["start"])
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if self._redirect_root_to_localhost(parsed.path):
            return
        if re.fullmatch(r"/api/documents/[A-Za-z0-9-]+/original", parsed.path):
            self.send_response(HTTPStatus.GONE)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        document_match = re.fullmatch(r"/api/documents/([A-Za-z0-9-]+)/preview", parsed.path)
        if document_match:
            self._serve_document(document_match.group(1), "preview", send_body=False)
            return
        self._serve_static(parsed.path, send_body=False)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if self._redirect_root_to_localhost(parsed.path):
            return
        if parsed.path == "/api/data":
            city = parse_qs(parsed.query).get("city", [""])[0]
            self._json(self.app.store.data(city))
            return
        if parsed.path == "/api/categories":
            city = parse_qs(parsed.query).get("city", [""])[0]
            self._json({"categories": self.app.store.categories(city)})
            return
        if parsed.path == "/api/link-reviews":
            query = parse_qs(parsed.query)
            try:
                self._json(self.app.store.link_review_queue(
                    query.get("status", ["pending"])[0], query.get("city", [""])[0],
                    query.get("summary", ["0"])[0] == "1",
                ))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/workbook-reviews":
            query = parse_qs(parsed.query)
            try:
                self._json(self.app.store.workbook_review_queue(
                    query.get("status", ["pending"])[0],
                    query.get("city", [""])[0],
                    query.get("summary", ["0"])[0] == "1",
                ))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/config":
            self._json({
                "adobe_pdf_embed_client_id": self.app.adobe_pdf_embed_client_id,
                "smart_search_model": getattr(self.app.store.gemini_client, "model", ""),
                "knowledge_chat_model": getattr(self.app.store.knowledge_gemini_client, "model", ""),
            })
            return
        conversation_match = re.fullmatch(r"/api/conversations/([A-Za-z0-9_-]+)", parsed.path)
        if conversation_match:
            try:
                self._json(self.app.store.knowledge_chat.conversation(conversation_match.group(1)))
            except KeyError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc.args[0]))
            return
        result_set_match = re.fullmatch(r"/api/result-sets/([A-Za-z0-9_-]+)/comments", parsed.path)
        if result_set_match:
            try:
                self._json(self.app.store.knowledge_chat.result_comments(result_set_match.group(1)))
            except KeyError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc.args[0]))
            return
        source_match = re.fullmatch(r"/api/sources/([A-Za-z0-9-]+)", parsed.path)
        if source_match:
            try:
                self._json(self.app.store.source_registry.public_source(source_match.group(1)))
            except (PermissionError, KeyError, FileNotFoundError, ValueError, RuntimeError) as exc:
                self._registry_error(exc)
            return
        document_match = re.fullmatch(r"/api/documents/([A-Za-z0-9-]+)/(preview|original|spreadsheet)", parsed.path)
        if document_match:
            document_id, action = document_match.groups()
            if action == "original":
                self._error(HTTPStatus.GONE, "Original-file downloads are disabled; use the in-app viewer")
                return
            if action == "preview":
                self._serve_document(document_id, action)
                return
            query = parse_qs(parsed.query)
            try:
                payload = self.app.store.source_registry.spreadsheet(
                    document_id,
                    query.get("sheet", [""])[0],
                    query.get("range", [""])[0],
                    int(query.get("page", ["1"])[0]),
                    int(query.get("page_size", ["100"])[0]),
                )
                self._json(payload)
            except (PermissionError, KeyError, FileNotFoundError, ValueError, RuntimeError, TypeError) as exc:
                self._registry_error(exc)
            return
        if parsed.path == "/source":
            self._error(HTTPStatus.GONE, "Filesystem source links were replaced by the in-app Source Viewer")
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/search":
                city = str(payload.get("city", ""))
                query = str(payload.get("query", ""))
                if not query.strip():
                    raise ValueError("Search query is required")
                if len(query) > 50_000:
                    raise ValueError("New comment is too long")
                limit = int(payload.get("limit", 5))
                result = self.app.store.gemini_search(
                    city, query, limit,
                    str(payload.get("discipline", "")),
                    str(payload.get("category", "")),
                )
                self._json(result)
                return
            if parsed.path == "/api/knowledge-chat":
                self._json(self.app.store.knowledge_chat.chat(payload))
                return
            if parsed.path == "/api/categories":
                comment_ids = payload.get("comment_ids", [])
                if not isinstance(comment_ids, list) or not all(isinstance(item, str) for item in comment_ids):
                    raise ValueError("comment_ids must be a list of strings")
                result = self.app.store.set_category(comment_ids, str(payload.get("category", "")))
                self._json(result)
                return
            if parsed.path == "/api/link-reviews":
                result = self.app.store.set_link_review(
                    str(payload.get("link_id", "")), str(payload.get("decision", "")),
                    str(payload.get("note", "")),
                )
                self._json(result)
                return
            if parsed.path == "/api/workbook-reviews":
                result = self.app.store.set_workbook_review(
                    str(payload.get("source_document", "")),
                    str(payload.get("decision", "")),
                    str(payload.get("note", "")),
                )
                self._json(result)
                return
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc.args[0] if exc.args else exc))
            return
        except (ValueError, TypeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except RuntimeError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return
        self._error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def _serve_static(self, request_path: str, send_body: bool = True) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (self.app.static_root / relative).resolve()
        try:
            candidate.relative_to(self.app.static_root)
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "Static file not found")
            return
        if not candidate.is_file():
            candidate = self.app.static_root / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}")


class PermitServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: DatasetStore, static_root: Path, adobe_pdf_embed_client_id: str = ""):
        self.store = store
        self.static_root = static_root.resolve()
        self.adobe_pdf_embed_client_id = adobe_pdf_embed_client_id
        super().__init__(address, PermitHandler)


def build_parser() -> argparse.ArgumentParser:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--categories", type=Path, default=workspace / "web_app" / "data" / "category_assignments.json")
    parser.add_argument("--source-root", type=Path, default=workspace / "comments&response")
    parser.add_argument("--static-root", type=Path, default=workspace / "web_app" / "static")
    parser.add_argument("--source-registry", type=Path, default=workspace / "web_app" / "data" / "source_registry.json")
    parser.add_argument("--preview-root", type=Path, default=workspace / "web_app" / "data" / "previews")
    parser.add_argument("--enrichment", type=Path, default=workspace / "web_app" / "data" / "gemini_enrichment.json")
    parser.add_argument("--search-index", type=Path, default=workspace / "web_app" / "data" / "search_index.json")
    parser.add_argument("--link-reviews", type=Path, default=workspace / "web_app" / "data" / "link_review_decisions.json")
    parser.add_argument("--workbook-reviews", type=Path, default=workspace / "web_app" / "data" / "workbook_review_decisions.json")
    parser.add_argument("--gemini-model", default=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"))
    parser.add_argument(
        "--knowledge-gemini-model",
        default=os.environ.get("KNOWLEDGE_GEMINI_MODEL", "gemini-3.1-flash-lite"),
        help="Gemini model used only for Knowledge Chat routing and grounded summaries",
    )
    parser.add_argument("--gemini-api-key-stdin", action="store_true", help="Read Gemini key from a hidden startup prompt")
    parser.add_argument(
        "--adobe-pdf-embed-client-id",
        default=os.environ.get("ADOBE_PDF_EMBED_CLIENT_ID", "da40245968664bb9bf47141e8e0e9195"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        shared_gemini_api_key = gemini_api_key()
        if not shared_gemini_api_key and args.gemini_api_key_stdin:
            shared_gemini_api_key = getpass.getpass("Gemini API key: ")
        gemini_client = GeminiClient(shared_gemini_api_key, args.gemini_model) if shared_gemini_api_key else None
        knowledge_gemini_client = GeminiClient(shared_gemini_api_key, args.knowledge_gemini_model) if shared_gemini_api_key else None
        store = DatasetStore(
            args.dataset, args.categories, args.source_root,
            args.source_registry, args.preview_root, args.enrichment, args.search_index,
            gemini_client=gemini_client,
            knowledge_gemini_client=knowledge_gemini_client,
            link_reviews_path=args.link_reviews,
            workbook_reviews_path=args.workbook_reviews,
        )
        server = PermitServer(
            (args.host, args.port), store, args.static_root,
            args.adobe_pdf_embed_client_id,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Unable to start permit browser: {exc}")
        return 2
    browser_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0", "::1"} else args.host
    print(f"Permit browser: http://{browser_host}:{args.port}")
    print(f"Dataset: {args.dataset.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
