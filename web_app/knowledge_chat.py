"""Constrained conversational layer over the existing permit-history store."""

from __future__ import annotations

import re
import secrets
import time
from collections import Counter
from typing import Any, Callable

try:
    from .data_trust import verified_text
except ImportError:
    from data_trust import verified_text


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
ALLOWED_FILTERS = {"city", "discipline", "review_round", "category", "response_status"}
GENERIC_QUERY_WORDS = {
    "a", "about", "all", "and", "are", "comment", "comments", "concern", "concerning",
    "count", "did", "do", "does", "find", "handled", "have", "historical", "history",
    "how", "in", "involving", "many", "of", "our", "permit", "please", "previously",
    "mention", "mentioned", "mentioning", "record", "records", "response", "responses", "show", "summarize", "the", "there",
    "these", "those", "to", "what", "with",
}
TERM_EQUIVALENTS = {
    "size": {"size", "dimension", "width"},
    "dimension": {"dimension", "size", "width"},
    "width": {"width", "dimension", "size"},
    "protection": {"protection", "protect", "removal", "arborist", "fencing"},
    "protect": {"protection", "protect", "removal", "arborist", "fencing"},
}


class PlanValidationError(ValueError):
    pass


def _clean_text(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _term_root(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


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
    return {
        "intent": intent,
        "subject": _clean_text(raw.get("subject"), 500),
        "operations": list(dict.fromkeys(operations)),
        "filters": safe_filters,
        "needs_clarification": bool(raw.get("needs_clarification")),
        "clarification_question": _clean_text(raw.get("clarification_question"), 300),
    }


def fallback_query_plan(message: str, has_previous: bool) -> dict[str, Any]:
    """Conservative local router used only when Gemini routing is unavailable."""
    lower = message.casefold()
    followup_reference = re.search(r"\b(only|those|these|them|without responses?|how did we respond|summarize those)\b", lower)
    if followup_reference:
        intent, operations = "filter_previous_results", ["load_previous_result_set"]
    elif re.search(r"\bhow many\b|\bcount\b|\bnumber of\b", lower):
        intent = "aggregate_count"
        operations = ["keyword_search", "count_parent_comments", "count_projects", "count_review_rounds"]
    elif re.search(r"\bcompare\b|\bdifferences?\b", lower):
        intent, operations = "compare_groups", ["smart_search", "group_by_city", "summarize_confirmed_responses"]
    elif re.search(r"\bsummar|\boverview|\bbreakdown|\bdistribution", lower):
        intent, operations = "topic_summary", ["load_filtered_comments", "group_by_discipline", "group_by_response_status", "summarize_confirmed_responses"]
    elif re.search(r"\brespond|\bhandled", lower):
        intent, operations = "historical_response_summary", ["smart_search", "summarize_confirmed_responses"]
    else:
        intent, operations = "precedent_search", ["smart_search"]
    subject_words = [
        word for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", lower)
        if word not in GENERIC_QUERY_WORDS
    ]
    return validate_query_plan({
        "intent": intent,
        "subject": " ".join(subject_words) or _clean_text(message),
        "operations": operations,
        "filters": {},
        "needs_clarification": bool(followup_reference and not has_previous),
        "clarification_question": "I do not have a previous verified result set to filter. What topic should I search first?" if followup_reference and not has_previous else "",
    })


class KnowledgeChat:
    """Execute constrained plans and retain short-lived, verified result sets."""

    def __init__(self, store: Any, ttl_seconds: int = 1800, clock: Callable[[], float] = time.time):
        self.store = store
        self.ttl_seconds = max(60, ttl_seconds)
        self.clock = clock
        self.result_sets: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}

    def _purge(self) -> None:
        now = self.clock()
        self.result_sets = {
            key: value for key, value in self.result_sets.items()
            if float(value["expires_at"]) > now
        }

    def _route(self, message: str, has_previous: bool) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        client = self.store.knowledge_gemini_client or self.store.gemini_client
        if client and hasattr(client, "plan_knowledge_query"):
            try:
                return validate_query_plan(client.plan_knowledge_query(message, has_previous)), warnings
            except (RuntimeError, ValueError) as exc:
                warnings.append(f"Gemini query routing was unavailable: {exc}")
        else:
            warnings.append("Gemini query routing is unavailable; a conservative local intent router was used.")
        return fallback_query_plan(message, has_previous), warnings

    def _record_matches_filters(self, row: dict[str, Any], filters: dict[str, str]) -> bool:
        for key in ("city", "discipline", "review_round", "category"):
            value = filters.get(key, "")
            actual = self.store._assignments.get(row["comment_id"], "Uncategorized") if key == "category" else row.get(key, "")
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
            "projects": Counter(str(row.get("property_project", "unknown")) for row in rows),
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
        terms = list(dict.fromkeys(
            _term_root(word) for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", subject.casefold())
            if word not in GENERIC_QUERY_WORDS and len(word) > 1
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
        client = self.store.knowledge_gemini_client
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

    def _literal_fallback(self, subject: str, filters: dict[str, str], unavailable: bool) -> tuple[list[str], dict[str, str], list[str]]:
        fallback = self._keyword_ids(subject, filters)
        verified_ids, verified_classes = self._chat_verify_literal_ids(subject, fallback)
        if verified_ids:
            reason = "Smart Search verification was unavailable" if unavailable else "Smart Search returned no verified precedent"
            return verified_ids, verified_classes, [
                f"{reason}; Gemini 3.1 Flash-Lite independently verified the bounded literal candidates used by this chat answer."
            ]
        return fallback, {item: "unverified" for item in fallback}, [
            "Semantic verification was unavailable. Literal database matches are shown as unverified evidence and are not used to claim historical company handling."
        ]

    def _smart_ids(self, subject: str, filters: dict[str, str]) -> tuple[list[str], dict[str, str], list[str]]:
        payload = self.store.gemini_search(
            filters.get("city", ""), subject, 15,
            filters.get("discipline", ""), filters.get("category", ""),
        )
        failures = payload.get("gemini_failures", [])
        if failures or payload.get("engine_label") != "Gemini accuracy-verified RAG":
            return self._literal_fallback(subject, filters, unavailable=True)
        classes = {
            str(item["comment_id"]): str(item["match_class"])
            for item in payload.get("results", [])
            if item.get("match_class") in {"direct", "related"}
        }
        ids = [comment_id for comment_id in classes if self._record_matches_filters(self.store._comments_by_id[comment_id], filters)]
        if not ids:
            return self._literal_fallback(subject, filters, unavailable=False)
        return ids, classes, ["Semantic summaries describe this independently verified evidence set; they are not presented as an exhaustive database count."]

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
            "projects": len({row.get("property_project", "") for row in rows}),
            "review_rounds": len({(row.get("property_project", ""), row.get("review_round", "")) for row in rows}),
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

    def _answer(self, message: str, plan: dict[str, Any], metrics: dict[str, int], rows: list[dict[str, Any]], breakdowns: dict[str, dict[str, int]]) -> dict[str, str]:
        subject = plan["subject"] or "the requested topic"
        if plan.get("evidence_scope") == "literal_unverified":
            database = (
                f"Database result: {metrics['parent_comments']} original parent comments across "
                f"{metrics['projects']} projects literally match the validated topic terms for {subject}."
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
        client = self.store.knowledge_gemini_client or self.store.gemini_client
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
                    pass
        elif plan.get("evidence_scope") == "literal_unverified" and rows:
            _, topics = self.store._common_topics(rows, limit=4)
            topic_rows = [{
                "representative_text": _clean_text(item["label"], 180),
                "parent_comments": int(item["occurrences"]),
            } for item in topics]
            pattern = (
                f"Comment evidence: these records concern {subject}, but semantic precedent verification did not complete. "
                f"{metrics['confirmed_responses']} have confirmed responses; no company-handling conclusion is drawn from unverified matches."
            )
            if client and hasattr(client, "summarize_database_scope"):
                try:
                    summary = _clean_text(client.summarize_database_scope(message, {
                        "exact_metrics": metrics,
                        "database_breakdowns": breakdowns,
                        "representative_recurring_topics": topic_rows,
                        "evidence_status": "literal matches; semantic precedent unverified",
                    }), 1800)
                    if summary:
                        pattern = f"Comment evidence: {summary} No company-handling conclusion is drawn from unverified matches."
                except RuntimeError:
                    pass
        if confirmed_rows and plan.get("evidence_scope") != "literal_unverified" and "load_filtered_comments" not in plan["operations"] and "summarize_confirmed_responses" in plan["operations"] and client and hasattr(client, "summarize_knowledge_evidence"):
            evidence = []
            for row in confirmed_rows[:15]:
                response = self._confirmed_response(row["comment_id"])
                evidence.append({
                    "city": row.get("city", ""), "discipline": row.get("discipline", ""),
                    "comment_text": verified_text(row),
                    "confirmed_response_text": verified_text(response) if response else "",
                })
            try:
                summary = _clean_text(client.summarize_knowledge_evidence(plan["subject"], evidence), 2000)
                if summary:
                    pattern = f"Historical pattern: {summary}"
            except RuntimeError:
                pass
        if plan["intent"] == "compare_groups":
            groups: dict[str, int] = {}
            for row in rows:
                groups[str(row.get("city", "unknown"))] = groups.get(str(row.get("city", "unknown")), 0) + 1
            pattern = "Database comparison: " + "; ".join(f"{name}: {count} parent comments" for name, count in sorted(groups.items())) + "."
        elif plan["intent"] == "explain_selected_comment" and rows:
            selected = rows[0]
            response = self._confirmed_response(selected["comment_id"])
            pattern = f"Direct historical quotation: “{_clean_text(selected.get('original_text'), 1200)}”"
            if response:
                pattern += f" Confirmed historical response: “{_clean_text(response.get('original_text'), 1200)}”"
        limitation = (
            f"Data limitation: {metrics['missing_responses']} comments have no stored response and "
            f"{metrics['unconfirmed_responses']} have a response link that is not confirmed. Those links were not used to describe company actions."
        )
        if not rows:
            pattern = "Historical evidence: No verified supporting precedent is available for this question."
        elif not confirmed_rows and plan.get("evidence_scope") != "literal_unverified":
            pattern = "Historical evidence: Matching comments were found, but none has a confirmed response that can support a statement about company actions."
        return {"database_result": database, "historical_pattern": pattern, "data_limitation": limitation}

    def chat(self, request: dict[str, Any]) -> dict[str, Any]:
        self._purge()
        message = _clean_text(request.get("message"), 4000)
        if not message:
            raise ValueError("Knowledge-chat message is required")
        conversation_id = _clean_text(request.get("conversation_id"), 120) or f"conv_{secrets.token_hex(8)}"
        previous_id = _clean_text(request.get("previous_result_set_id"), 120)
        selected_comment_id = _clean_text(request.get("selected_comment_id"), 160)
        previous = self.result_sets.get(previous_id) if previous_id else None
        if previous_id and not previous:
            raise KeyError("Previous result set was not found or has expired")
        plan, warnings = self._route(message, bool(previous))
        request_filters = request.get("filters", {}) if isinstance(request.get("filters", {}), dict) else {}
        filters = {key: _clean_text(value, 120) for key, value in request_filters.items() if key in ALLOWED_FILTERS and _clean_text(value, 120)}
        city = _clean_text(request.get("city_id"), 120)
        named_cities = [item["name"] for item in self.store.cities() if item["name"].casefold() in message.casefold()]
        if city and not (plan["intent"] == "compare_groups" and len(named_cities) >= 2):
            filters["city"] = city
        filters.update(self._validated_model_filters(plan["filters"]))
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
            if "without response" in lower:
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
            comment_ids, classes, search_warnings = self._smart_ids(plan["subject"] or message, filters)
            warnings.extend(search_warnings)
            if comment_ids and all(classes.get(item) == "unverified" for item in comment_ids):
                plan["evidence_scope"] = "literal_unverified"
        else:
            comment_ids = self._keyword_ids(plan["subject"] or message, filters)
            classes = {item: "direct" for item in comment_ids}

        comment_ids = list(dict.fromkeys(item for item in comment_ids if item in self.store._comments_by_id))
        direct_ids = [item for item in comment_ids if classes.get(item) == "direct"]
        related_ids = [item for item in comment_ids if classes.get(item) == "related"]
        metrics = self._metrics(comment_ids)
        rows = [self.store._comments_by_id[item] for item in comment_ids]
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
        self.result_sets[result_set_id] = result_set
        sections = self._answer(message, plan, metrics, rows, breakdowns)
        action_label = f"Show {metrics['parent_comments']} matching comment{'s' if metrics['parent_comments'] != 1 else ''}"
        response = {
            "conversation_id": conversation_id, "answer": "\n\n".join(sections.values()),
            "answer_sections": sections, "intent": plan["intent"], "result_set_id": result_set_id,
            "metrics": metrics, "citations": self._citations(comment_ids),
            "breakdowns": breakdowns,
            "actions": [{"type": "show_results", "label": action_label, "result_set_id": result_set_id}],
            "warnings": warnings, "query_plan": plan, "needs_clarification": False,
        }
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
