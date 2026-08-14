"""Detect source files copied within the same site and review round.

Folder names are useful context, but they are not reliable document identity.
This module uses immutable file hashes first, then the document's own date and
comment-set signature. A repeated comment in a different review round remains
a separate historical occurrence even when the source bytes are identical.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

try:
    from .comment_dedup import normalized_comment_text, site_identity
except ImportError:
    from comment_dedup import normalized_comment_text, site_identity


_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        ("January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"),
        start=1,
    )
}
_MONTH_ALIASES = {
    **_MONTHS,
    **{name[:3].casefold(): index for name, index in _MONTHS.items()},
}


def _first_path(value: Any) -> str:
    return str(value or "").split(" | ", 1)[0].strip()


def _parse_date(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    iso = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            pass
    numeric = re.search(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})\b", text)
    if numeric:
        year = int(numeric.group(3))
        year += 2000 if year < 100 else 0
        try:
            return date(year, int(numeric.group(1)), int(numeric.group(2))).isoformat()
        except ValueError:
            pass
    words = re.search(
        r"\b([A-Za-z]{3,9})\s+(\d{1,2})\s*,?\s+(20\d{2})\b", text,
    )
    if words:
        month = _MONTH_ALIASES.get(words.group(1).casefold())
        if month:
            try:
                return date(int(words.group(3)), month, int(words.group(2))).isoformat()
            except ValueError:
                pass
    return ""


def _visible_date(text: str) -> tuple[str, str]:
    # Search the visible document in reading order. The first date in these
    # city comment letters is the letter/report date, before referenced plan
    # dates later in the header.
    compact = re.sub(r"\s+", " ", text or "")
    patterns = [
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s*,?\s+20\d{2}\b",
        r"\b\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*20\d{2}\b",
    ]
    matches: list[tuple[int, str]] = []
    for pattern in patterns:
        matches.extend((match.start(), match.group(0)) for match in re.finditer(pattern, compact, re.I))
    for _position, value in sorted(matches):
        parsed = _parse_date(value)
        if parsed:
            return parsed, value
    return "", ""


def _docx_date(path: Path) -> tuple[str, str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            core_text = ""
            if "docProps/core.xml" in archive.namelist():
                root = ET.fromstring(archive.read("docProps/core.xml"))
                core_text = " ".join(node.text or "" for node in root if node.text)
            if "word/document.xml" not in archive.namelist():
                return "", "", ""
            root = ET.fromstring(archive.read("word/document.xml"))
            visible = " ".join(
                node.text or ""
                for node in root.iter()
                if node.tag.rsplit("}", 1)[-1] in {"t", "instrText"}
            )
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ET.ParseError):
        return "", "", ""
    parsed, evidence = _visible_date(visible)
    if parsed:
        return parsed, evidence, "visible_document_date"
    parsed, evidence = _visible_date(core_text)
    if parsed:
        return parsed, evidence, "docx_core_metadata"
    return "", "", "missing"


def document_date(
    path: Path,
    records: list[dict[str, Any]],
) -> tuple[str, str, str]:
    """Return ISO date, evidence, and method without using filesystem mtime."""
    for record in records:
        for field in ("document_date", "source_document_date", "report_date", "letter_date"):
            value = record.get(field)
            if isinstance(value, dict):
                parsed = _parse_date(value.get("iso") or value.get("raw") or value.get("value"))
                evidence = str(value.get("evidence") or value.get("raw") or value.get("iso") or "")
                source = str(value.get("source") or field)
            else:
                parsed = _parse_date(value)
                evidence = str(value or "")
                source = field
            if parsed:
                return parsed, evidence, f"record.{source}"
    if path.suffix.casefold() == ".docx":
        return _docx_date(path)
    return "", "", "missing"


def _round_number(value: Any) -> int:
    matches = re.findall(r"\d+", str(value or ""))
    return int(matches[-1]) if matches else 10**9


def _content_signature(records: list[dict[str, Any]]) -> str:
    values = sorted({
        normalized_comment_text(record)
        for record in records
        if record.get("duplicate_status") != "hierarchical_subpoint"
        and normalized_comment_text(record)
    })
    if not values:
        return ""
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def _winner_key(info: dict[str, Any], links: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    rows = info["records"]
    confirmed = sum(
        links.get(str(row.get("comment_id", "")), {}).get("review_status") == "confirmed"
        for row in rows
    )
    responses = sum(bool(row.get("response_id")) for row in rows)
    return (-int(confirmed), -int(responses), _round_number(info["round"]), info["path"])


def _match_duplicate_row(
    row: dict[str, Any],
    winner_rows: list[dict[str, Any]],
    used: set[str],
) -> dict[str, Any] | None:
    text = normalized_comment_text(row)
    candidates = [
        candidate for candidate in winner_rows
        if str(candidate.get("comment_id", "")) not in used
        and normalized_comment_text(candidate) == text
        and str(candidate.get("discipline", "")) == str(row.get("discipline", ""))
    ]
    if not candidates:
        candidates = [
            candidate for candidate in winner_rows
            if str(candidate.get("comment_id", "")) not in used
            and normalized_comment_text(candidate) == text
        ]
    return candidates[0] if candidates else None


def mark_copied_source_documents(
    dataset: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    """Suppress source-file copies within one round while retaining audit rows."""
    comments = dataset.get("comments", [])
    # Re-running after an older pipeline must undo its cross-round aliases.
    for record in comments:
        prior_lineage_duplicate = bool(record.pop("lineage_duplicate_of", None))
        for field in (
            "source_lineage_id", "source_lineage_role", "source_lineage_reason",
            "source_aliases",
        ):
            record.pop(field, None)
        if prior_lineage_duplicate and record.get("duplicate_status") == "copied_source_cross_round":
            record.pop("duplicate_of", None)
            record.pop("duplicate_status", None)
            if record.get("text_trust_status") == "verified":
                record["search_eligible"] = True
    links = {
        str(row.get("comment_id", "")): row
        for row in dataset.get("comment_response_links", [])
    }
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in comments:
        path = _first_path(record.get("source_document"))
        if path:
            by_path[path].append(record)
    infos: list[dict[str, Any]] = []
    for path, rows in by_path.items():
        source_path = Path(path)
        if not source_path.is_absolute():
            source_path = workspace / source_path
        digest = str(next((row.get("source_sha256") for row in rows if row.get("source_sha256")), ""))
        parsed_date, evidence, method = document_date(source_path, rows)
        for row in rows:
            row["source_document_date"] = parsed_date
            row["source_date_evidence"] = evidence
            row["source_date_method"] = method
        infos.append({
            "path": path,
            "path_key": path.casefold(),
            "rows": rows,
            "records": rows,
            "hash": digest,
            "date": parsed_date,
            "date_evidence": evidence,
            "date_method": method,
            "site": site_identity(rows[0]),
            "round": str(rows[0].get("review_round", "")),
            "filename": Path(path).name.casefold(),
            "content_signature": _content_signature(rows),
        })

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for info in infos:
        if info["hash"]:
            groups[("hash", info["site"], info["round"], info["hash"])].append(info)
    # A re-saved copy has a new hash. Only consider it a copy when the
    # document's own date and complete extracted comment set agree.
    for info in infos:
        if info["date"] and info["content_signature"]:
            # Filenames are not document identity.  A re-exported copy may be
            # renamed while retaining the same dated comment content.
            groups[(
                "dated-content", info["site"], info["round"],
                info["date"], info["content_signature"],
            )].append(info)

    report: dict[str, Any] = {
        "copied_source_groups": 0,
        "copied_source_paths_suppressed": 0,
        "copied_comment_rows_suppressed": 0,
        "copied_source_details": [],
    }
    seen_group_signatures: set[tuple[str, ...]] = set()
    lineage_rows: dict[str, dict[str, Any]] = {}
    for key, candidates in groups.items():
        paths = tuple(sorted({info["path"] for info in candidates}))
        rounds = {str(info["round"]) for info in candidates}
        # A byte-identical source is one document only inside the same review
        # round. Different rounds are distinct historical occurrences.
        if len(paths) < 2 or paths in seen_group_signatures:
            continue
        seen_group_signatures.add(paths)
        # Exact hash is definitive. Dated-content requires a reliable date;
        # missing dates never trigger cross-round suppression.
        reason = "identical_sha256" if key[0] == "hash" else "same_internal_date_and_comment_set"
        if key[0] != "hash" and any(not info["date"] for info in candidates):
            continue
        winner_info = min(candidates, key=lambda info: _winner_key(info, links))
        winner_rows = winner_info["rows"]
        aliases = sorted(paths)
        lineage_id = "L-" + hashlib.sha256(
            (winner_info["site"] + "|" + "|".join(aliases)).encode("utf-8")
        ).hexdigest()[:20]
        lineage = {
            "lineage_id": lineage_id,
            "canonical_source_document": winner_info["path"],
            "source_aliases": aliases,
            "document_date": winner_info["date"],
            "date_evidence": winner_info["date_evidence"],
            "date_method": winner_info["date_method"],
            "reason": reason,
            "review_rounds_seen": sorted(rounds, key=_round_number),
        }
        lineage_rows[lineage_id] = lineage
        used: set[str] = set()
        for info in candidates:
            for row in info["rows"]:
                row["source_lineage_id"] = lineage_id
                row["source_lineage_role"] = "canonical" if info is winner_info else "copied_alias"
                row["source_lineage_reason"] = reason
                row["source_aliases"] = aliases
                if info is winner_info:
                    continue
                match = _match_duplicate_row(row, winner_rows, used)
                if match:
                    match_id = str(match.get("comment_id", ""))
                    used.add(match_id)
                else:
                    match_id = str(winner_rows[0].get("comment_id", "")) if winner_rows else ""
                if match_id:
                    row["lineage_duplicate_of"] = match_id
                    if row.get("duplicate_status") != "hierarchical_subpoint":
                        row["duplicate_of"] = match_id
                        row["duplicate_status"] = "copied_source_same_round"
                    row["search_eligible"] = False
                    report["copied_comment_rows_suppressed"] += 1
        for row in winner_rows:
            row["source_lineage_id"] = lineage_id
            row["source_lineage_role"] = "canonical"
            row["source_lineage_reason"] = reason
            row["source_aliases"] = aliases
        report["copied_source_groups"] += 1
        report["copied_source_paths_suppressed"] += len(paths) - 1
        report["copied_source_details"].append(lineage)

    dataset["source_lineage_groups"] = lineage_rows
    return report
