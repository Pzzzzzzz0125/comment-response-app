#!/usr/bin/env python3
"""Enrich every extracted comment/response with Gemini, with resumable caching."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROMPT_VERSION = "1.0"
DEFAULT_MODEL = "gemini-3.5-flash"

OUTPUT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "display_text": {"type": "STRING"},
        "blocks": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "kind": {"type": "STRING", "enum": ["paragraph", "list"]},
                    "title": {"type": "STRING"},
                    "text": {"type": "STRING"},
                    "items": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["kind", "title", "text", "items"],
            },
        },
        "secondary_references": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "kind": {"type": "STRING", "enum": ["sheet", "document", "detail", "attachment", "other"]},
                    "sheet": {"type": "STRING"},
                    "document_hint": {"type": "STRING"},
                    "evidence_query": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["kind", "sheet", "document_hint", "evidence_query", "reason", "confidence"],
            },
        },
    },
    "required": ["display_text", "blocks", "secondary_references"],
}

SEARCH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "comment_id": {"type": "STRING"},
                    "score": {"type": "NUMBER"},
                    "required_action_matches": {"type": "BOOLEAN"},
                    "important_difference": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["comment_id", "score", "required_action_matches", "important_difference", "reason"],
            },
        }
    },
    "required": ["results"],
}

QUERY_ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "search_goal": {"type": "STRING"},
        "primary_subject": {"type": "STRING"},
        "secondary_subjects": {"type": "ARRAY", "items": {"type": "STRING"}},
        "condition_or_problem": {"type": "STRING"},
        "regulatory_concern": {"type": "STRING"},
        "requested_actions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "issue_type": {"type": "STRING"},
        "city": {"type": "STRING"},
        "discipline": {"type": "STRING"},
        "code_sections": {"type": "ARRAY", "items": {"type": "STRING"}},
        "technical_terms": {"type": "ARRAY", "items": {"type": "STRING"}},
        "required_concepts": {"type": "ARRAY", "items": {"type": "STRING"}},
        "optional_concepts": {"type": "ARRAY", "items": {"type": "STRING"}},
        "excluded_concepts": {"type": "ARRAY", "items": {"type": "STRING"}},
        "direct_match_definition": {"type": "STRING"},
        "related_match_definition": {"type": "STRING"},
        "ambiguities": {"type": "ARRAY", "items": {"type": "STRING"}},
        "semantic_query": {"type": "STRING"},
    },
    "required": ["search_goal", "primary_subject", "secondary_subjects", "condition_or_problem", "regulatory_concern", "requested_actions", "issue_type", "city", "discipline", "code_sections", "technical_terms", "required_concepts", "optional_concepts", "excluded_concepts", "direct_match_definition", "related_match_definition", "ambiguities", "semantic_query"],
}

REWRITE_SCHEMA = {"type": "OBJECT", "properties": {"rewrites": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}, "kind": {"type": "STRING"}, "preserves_meaning": {"type": "BOOLEAN"}}, "required": ["query", "kind", "preserves_meaning"]}}}, "required": ["rewrites"]}

CANDIDATE_EVALUATION_SCHEMA = {"type": "OBJECT", "properties": {"results": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "candidate_id": {"type": "STRING"}, "subject_match": {"type": "STRING", "enum": ["exact", "partial", "different", "uncertain"]},
    "condition_match": {"type": "STRING", "enum": ["exact", "partial", "different", "uncertain"]}, "action_match": {"type": "STRING", "enum": ["exact", "partial", "different", "uncertain"]},
    "regulatory_match": {"type": "STRING", "enum": ["exact", "partial", "different", "uncertain"]}, "jurisdiction_compatibility": {"type": "STRING", "enum": ["same", "compatible", "different", "unknown"]},
    "match_class": {"type": "STRING", "enum": ["direct", "related", "unrelated", "uncertain"]}, "contradictions": {"type": "ARRAY", "items": {"type": "STRING"}},
    "important_differences": {"type": "ARRAY", "items": {"type": "STRING"}}, "relevance_score": {"type": "NUMBER"}, "confidence": {"type": "NUMBER"}, "reason": {"type": "STRING"},
}, "required": ["candidate_id", "subject_match", "condition_match", "action_match", "regulatory_match", "jurisdiction_compatibility", "match_class", "contradictions", "important_differences", "relevance_score", "confidence", "reason"]}}}, "required": ["results"]}

FINAL_RANK_SCHEMA = {"type": "OBJECT", "properties": {"results": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "candidate_id": {"type": "STRING"}, "match_class": {"type": "STRING", "enum": ["direct", "related", "unrelated", "uncertain"]},
    "relevance_score": {"type": "NUMBER"}, "confidence": {"type": "NUMBER"}, "response_applicable": {"type": "BOOLEAN"},
    "important_differences": {"type": "ARRAY", "items": {"type": "STRING"}}, "reason": {"type": "STRING"},
}, "required": ["candidate_id", "match_class", "relevance_score", "confidence", "response_applicable", "important_differences", "reason"]}}}, "required": ["results"]}

SYSTEM_INSTRUCTION = """You organize permit-review evidence without changing its meaning.

For the supplied single comment or response:
1. Fix extraction artifacts, missing spaces, accidental line breaks, and punctuation. Preserve every technical fact, code citation, sheet/detail identifier, measurement, and qualification. Do not add facts or advice.
2. Return attractive display blocks. Use a list only when the source actually contains multiple requirements/items; otherwise use one or more concise paragraphs.
3. Identify every secondary source referenced or implied by phrases such as refer to, see, added on, updated on, attached, included, provided, or submitted. A secondary source is not the comment/response letter itself. Choose an exact candidate filename only when supported by the candidate list. For plan sheets, return the sheet identifier and a short evidence query describing the actual note/change that should be located on that sheet.
4. If no secondary source is indicated, return an empty secondary_references array.

Never invent a source, sheet, quote, or filename. Confidence must be between 0 and 1."""


def record_digest(record: dict[str, Any]) -> str:
    relevant = {
        "id": record.get("comment_id") or record.get("response_id"),
        "text": record.get("original_text", ""),
        "source_document": record.get("source_document", ""),
        "source_location": record.get("source_location", ""),
        "source_sheet": record.get("source_sheet", ""),
        "source_page": record.get("source_page", ""),
    }
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_result(value: dict[str, Any], original_text: str) -> dict[str, Any]:
    display_text = str(value.get("display_text", "")).strip() or str(original_text or "").strip()
    blocks: list[dict[str, Any]] = []
    for block in value.get("blocks", []) if isinstance(value.get("blocks"), list) else []:
        if not isinstance(block, dict) or block.get("kind") not in {"paragraph", "list"}:
            continue
        items = [str(item).strip() for item in block.get("items", []) if str(item).strip()]
        text = str(block.get("text", "")).strip()
        if block["kind"] == "paragraph" and not text:
            continue
        if block["kind"] == "list" and not items:
            continue
        blocks.append({
            "kind": block["kind"],
            "title": str(block.get("title", "")).strip(),
            "text": text,
            "items": items,
        })
    if not blocks:
        blocks = [{"kind": "paragraph", "title": "", "text": display_text, "items": []}]
    references: list[dict[str, Any]] = []
    for reference in value.get("secondary_references", []) if isinstance(value.get("secondary_references"), list) else []:
        if not isinstance(reference, dict):
            continue
        try:
            confidence = max(0.0, min(1.0, float(reference.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        item = {
            "kind": str(reference.get("kind", "other")),
            "sheet": str(reference.get("sheet", "")).strip().upper(),
            "document_hint": str(reference.get("document_hint", "")).strip(),
            "evidence_query": str(reference.get("evidence_query", "")).strip(),
            "reason": str(reference.get("reason", "")).strip(),
            "confidence": confidence,
        }
        if confidence >= 0.55 and (item["sheet"] or item["document_hint"]):
            references.append(item)
    return {"display_text": display_text, "blocks": blocks, "secondary_references": references}


class GeminiClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: int = 90):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def list_models(self) -> list[str]:
        request = Request(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
            headers={"x-goog-api-key": self.api_key},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, OSError, URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to list Gemini models: {exc}") from exc
        return [
            str(model.get("name", "")).removeprefix("models/")
            for model in body.get("models", [])
            if "generateContent" in model.get("supportedGenerationMethods", [])
        ]

    def enrich(self, record: dict[str, Any], record_type: str, candidates: list[str]) -> dict[str, Any]:
        context = {
            "record_type": record_type,
            "record_id": record.get("comment_id") if record_type == "comment" else record.get("response_id"),
            "discipline": record.get("discipline", ""),
            "original_text": record.get("original_text", ""),
            "source_location": record.get("source_location", ""),
            "candidate_documents_in_same_project": candidates,
        }
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(context, ensure_ascii=False)}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": OUTPUT_SCHEMA,
            },
        }
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model, safe='')}:generateContent"
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        body: dict[str, Any] | None = None
        for attempt in range(5):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                if exc.code == 429 and "prepayment credits are depleted" in detail.casefold():
                    raise RuntimeError(f"Gemini HTTP {exc.code}: prepaid credits are depleted") from exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                    raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
                time.sleep(min(30, 2 ** attempt * 2))
            except (OSError, URLError, json.JSONDecodeError) as exc:
                if attempt == 4:
                    raise RuntimeError(f"Gemini request failed: {exc}") from exc
                time.sleep(min(30, 2 ** attempt * 2))
        if body is None:
            raise RuntimeError("Gemini request produced no response")
        try:
            raw = body["candidates"][0]["content"]["parts"][0]["text"]
            return normalize_result(json.loads(raw), str(record.get("original_text", "")))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Gemini returned no valid structured result") from exc

    def _structured(self, instruction: str, context: dict[str, Any], schema: dict[str, Any], timeout: int = 25) -> dict[str, Any]:
        context = {
            **context,
        }
        payload = {
            "systemInstruction": {"parts": [{"text": instruction}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(context, ensure_ascii=False)}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model, safe='')}:generateContent"
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=min(self.timeout, timeout)) as response:
                    body = json.loads(response.read().decode("utf-8"))
                raw = body["candidates"][0]["content"]["parts"][0]["text"]
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise TypeError("structured result is not an object")
                return result
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                if exc.code == 429 and "prepayment credits are depleted" in detail.casefold():
                    raise RuntimeError("Gemini prepaid credits are depleted") from exc
                if attempt == 1 or exc.code not in {429, 500, 502, 503, 504}:
                    raise RuntimeError(f"Gemini search HTTP {exc.code}: {detail}") from exc
                time.sleep(1)
            except (OSError, URLError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                if attempt == 1:
                    raise RuntimeError(f"Gemini smart search failed: {exc}") from exc
                time.sleep(1)
        raise RuntimeError("Gemini smart search produced no response")

    def analyze_search_query(self, query: str) -> dict[str, Any]:
        instruction = """Analyze one permit-review search query for accuracy-first precedent retrieval. Extract only supported information; use empty strings or arrays when unknown. Required concepts must be present for a direct match. Optional concepts may help ranking. Excluded concepts identify meanings that would contradict the query. Define direct versus merely related without inventing requirements."""
        return self._structured(instruction, {"query": query}, QUERY_ANALYSIS_SCHEMA)

    def rewrite_search_query(self, query: str, analysis: dict[str, Any]) -> list[str]:
        instruction = """Create meaning-preserving search rewrites for permit records: close paraphrases, likely city-review terminology, alternative technical terminology, expanded abbreviations, and alternative descriptions of the requested action. Never broaden or change the underlying condition, regulatory concern, or requested action. Mark unsafe rewrites false."""
        result = self._structured(instruction, {"original_query": query, "query_analysis": analysis, "maximum_rewrites": 8}, REWRITE_SCHEMA)
        seen = {query.casefold().strip()}
        rows = []
        for item in result.get("rewrites", []):
            value = str(item.get("query", "")).strip()
            key = value.casefold()
            if item.get("preserves_meaning") is True and value and key not in seen:
                seen.add(key)
                rows.append(value)
        return rows[:8]

    def embed_documents(self, texts: list[str], model: str = "gemini-embedding-001") -> list[list[float]]:
        if not texts:
            return []
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:batchEmbedContents"
        payload = {
            "requests": [{
                "model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
                "taskType": "RETRIEVAL_DOCUMENT",
            } for text in texts]
        }
        request = Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key}, method="POST")
        try:
            with urlopen(request, timeout=min(self.timeout, 30)) as response:
                body = json.loads(response.read().decode("utf-8"))
            return [[float(value) for value in item["values"]] for item in body["embeddings"]]
        except (HTTPError, OSError, URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Gemini embedding failed: {exc}") from exc

    def embed_query(self, text: str, model: str = "gemini-embedding-001") -> list[float]:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:embedContent"
        payload = {"model": f"models/{model}", "content": {"parts": [{"text": text}]}, "taskType": "RETRIEVAL_QUERY"}
        request = Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key}, method="POST")
        try:
            with urlopen(request, timeout=min(self.timeout, 20)) as response:
                body = json.loads(response.read().decode("utf-8"))
            return [float(value) for value in body["embedding"]["values"]]
        except (HTTPError, OSError, URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Gemini query embedding failed: {exc}") from exc

    def rerank(self, query_analysis: dict[str, Any], candidates: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
        instruction = """Rerank only the supplied historical permit-comment candidates by semantic relevance. Give priority to the same subject, requirement, and required action. Identify meaningful technical differences. Never invent an ID, response, fact, or citation. Return no result when no precedent is sufficiently similar."""
        result = self._structured(instruction, {
            "query_analysis": query_analysis,
            "maximum_results": max(1, min(limit, 10)),
            "candidate_comments": candidates[:20],
        }, SEARCH_SCHEMA)
        allowed = {str(item.get("comment_id", "")) for item in candidates}
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in result.get("results", []):
            comment_id = str(item.get("comment_id", ""))
            if comment_id not in allowed or comment_id in seen:
                continue
            try:
                score = max(0.0, min(1.0, float(item.get("score", 0))))
            except (TypeError, ValueError):
                continue
            if score < 0.2:
                continue
            seen.add(comment_id)
            rows.append({
                "comment_id": comment_id,
                "score": round(score, 4),
                "required_action_matches": bool(item.get("required_action_matches")),
                "important_difference": str(item.get("important_difference", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
            })
        rows.sort(key=lambda item: (-item["score"], item["comment_id"]))
        return rows[: max(1, min(limit, 10))]

    def evaluate_search_candidates(self, analysis: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        instruction = """Evaluate each supplied permit-comment candidate independently. Embedding or vocabulary overlap is not proof of equivalence. Compare subject, condition or deficiency, requested action, regulatory concern, and jurisdiction. A direct match requires the same underlying issue and requested action. Related means useful context but not equivalent. Unrelated candidates must be labeled unrelated. Use only supplied IDs."""
        result = self._structured(instruction, {"query_analysis": analysis, "candidate_summaries": candidates}, CANDIDATE_EVALUATION_SCHEMA, timeout=45)
        allowed = {str(item.get("candidate_id", "")) for item in candidates}
        return [item for item in result.get("results", []) if str(item.get("candidate_id", "")) in allowed]

    def deep_rerank(self, analysis: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        instruction = """Deeply rank the supplied full historical permit records. Decide whether each has the same subject, condition, requested action, and regulatory concern, and whether its historical response is applicable. Generic shared words must not dominate. Direct means genuinely equivalent; related means useful but materially different; unrelated must be removed. Use only supplied IDs and facts."""
        result = self._structured(instruction, {"query_analysis": analysis, "full_candidates": candidates}, FINAL_RANK_SCHEMA, timeout=60)
        allowed = {str(item.get("candidate_id", "")) for item in candidates}
        rows = [item for item in result.get("results", []) if str(item.get("candidate_id", "")) in allowed]
        return sorted(rows, key=lambda item: (-float(item.get("relevance_score", 0)), str(item.get("candidate_id", ""))))

    def verify_search_results(self, analysis: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        instruction = """Act as an independent conservative verifier of proposed permit precedents. Confirm whether every direct result is truly direct, downgrade to related when materially different, and remove unrelated results. Check for omitted contradictions and important differences. Returning zero results is preferable to a misleading precedent. You may only return supplied IDs and may not invent responses or citations."""
        result = self._structured(instruction, {"query_analysis": analysis, "proposed_results": candidates}, FINAL_RANK_SCHEMA, timeout=45)
        allowed = {str(item.get("candidate_id", "")) for item in candidates}
        rows = [item for item in result.get("results", []) if str(item.get("candidate_id", "")) in allowed and item.get("match_class") in {"direct", "related"}]
        return sorted(rows, key=lambda item: (0 if item.get("match_class") == "direct" else 1, -float(item.get("relevance_score", 0))))

    # Backward-compatible bounded alias for integrations written against the earlier client.
    def search(self, query: str, candidates: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
        return self.rerank({"semantic_query": query}, candidates[:20], limit)


def candidate_documents(source_root: Path, record: dict[str, Any]) -> list[str]:
    raw = str(record.get("source_document", "")).split(" | ")[0].strip()
    parts = Path(raw).parts
    if len(parts) < 2:
        return []
    project = parts[1].casefold()
    names = {
        path.name for path in source_root.rglob("*")
        if path.is_file() and len(path.relative_to(source_root.parent).parts) > 1
        and path.relative_to(source_root.parent).parts[1].casefold() == project
    }
    return sorted(names, key=str.casefold)


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def run_enrichment(
    dataset_path: Path,
    source_root: Path,
    output_path: Path,
    client: GeminiClient,
    record_ids: set[str] | None = None,
    limit: int = 0,
    force: bool = False,
    delay: float = 0.15,
    workers: int = 4,
    record_types: set[str] | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, int]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if output_path.is_file():
        cache = json.loads(output_path.read_text(encoding="utf-8"))
    else:
        cache = {"schema_version": "1.0", "prompt_version": PROMPT_VERSION, "model": client.model, "entries": {}}
    entries = cache.setdefault("entries", {})
    cache.update({"schema_version": "1.0", "prompt_version": PROMPT_VERSION, "model": client.model})
    records: list[tuple[str, dict[str, Any], str]] = []
    for collection, record_type, id_field in (("comments", "comment", "comment_id"), ("responses", "response", "response_id")):
        records.extend((str(record[id_field]), record, record_type) for record in dataset.get(collection, []))
    processed = skipped = failed = 0
    pending: list[tuple[str, dict[str, Any], str, str]] = []
    for record_id, record, record_type in records:
        if record_types and record_type not in record_types:
            continue
        if record_ids and record_id not in record_ids:
            continue
        digest = record_digest(record)
        existing = entries.get(record_id, {})
        if not force and existing.get("input_sha256") == digest and existing.get("prompt_version") == PROMPT_VERSION and existing.get("model") == client.model:
            skipped += 1
            continue
        if limit and len(pending) >= limit:
            break
        pending.append((record_id, record, record_type, digest))

    def enrich_one(item: tuple[str, dict[str, Any], str, str]) -> tuple[str, str, str, dict[str, Any]]:
        record_id, record, record_type, digest = item
        if delay:
            time.sleep(delay)
        result = client.enrich(record, record_type, candidate_documents(source_root, record))
        return record_id, record_type, digest, result

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        futures = {executor.submit(enrich_one, item): item[0] for item in pending}
        for future in as_completed(futures):
            record_id = futures[future]
            try:
                record_id, record_type, digest, result = future.result()
            except (OSError, RuntimeError, ValueError) as exc:
                failed += 1
                progress(f"FAILED {record_id}: {exc}")
                continue
            entries[record_id] = {
                "record_type": record_type,
                "input_sha256": digest,
                "prompt_version": PROMPT_VERSION,
                "model": client.model,
                **result,
            }
            processed += 1
            write_cache(output_path, cache)
            progress(f"{processed}: enriched {record_id}")
    return {"processed": processed, "skipped": skipped, "failed": failed, "total_entries": len(entries)}


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--source-root", type=Path, default=workspace / "comments&response")
    parser.add_argument("--output", type=Path, default=workspace / "web_app" / "data" / "gemini_enrichment.json")
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--record-id", action="append", default=[])
    parser.add_argument("--record-type", action="append", choices=["comment", "response"], default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--api-key-stdin", action="store_true", help="Read the API key from a hidden interactive prompt")
    parser.add_argument("--list-models", action="store_true", help="List models that support generateContent and exit")
    args = parser.parse_args()
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    if not api_key and args.api_key_stdin:
        api_key = getpass.getpass("Gemini API key: ")
    if not api_key:
        parser.error("Set GEMINI_API_KEY (or GOOGLE_API_KEY) before running enrichment")
    client = GeminiClient(api_key, args.model)
    if args.list_models:
        print("\n".join(client.list_models()))
        return 0
    result = run_enrichment(
        args.dataset, args.source_root, args.output, client,
        set(args.record_id) or None, args.limit, args.force, args.delay, args.workers, set(args.record_type) or None,
    )
    print(json.dumps(result, sort_keys=True))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
