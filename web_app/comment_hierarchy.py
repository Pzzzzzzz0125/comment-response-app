"""Preserve Word list hierarchy when one numbered comment has subpoints."""

from __future__ import annotations

import copy
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attribute(node: ET.Element | None, name: str, default: str = "") -> str:
    if node is None:
        return default
    return next(
        (str(value) for key, value in node.attrib.items() if _local(key) == name),
        default,
    )


def _numbering_definitions(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, str], dict[tuple[str, int], dict[str, Any]]]:
    if "word/numbering.xml" not in archive.namelist():
        return {}, {}
    root = ET.fromstring(archive.read("word/numbering.xml"))
    abstract_levels: dict[tuple[str, int], dict[str, Any]] = {}
    number_to_abstract: dict[str, str] = {}
    for node in root:
        if _local(node.tag) == "abstractNum":
            abstract_id = _attribute(node, "abstractNumId")
            for level_node in node:
                if _local(level_node.tag) != "lvl":
                    continue
                level = int(_attribute(level_node, "ilvl", "0") or 0)
                values = {_local(child.tag): _attribute(child, "val") for child in level_node}
                abstract_levels[(abstract_id, level)] = {
                    "start": int(values.get("start") or 1),
                    "format": values.get("numFmt") or "decimal",
                    "template": values.get("lvlText") or f"%{level + 1}.",
                }
        elif _local(node.tag) == "num":
            number_id = _attribute(node, "numId")
            abstract = next(
                (
                    _attribute(child, "val")
                    for child in node
                    if _local(child.tag) == "abstractNumId"
                ),
                "",
            )
            if number_id and abstract:
                number_to_abstract[number_id] = abstract
    return number_to_abstract, abstract_levels


def _alpha(value: int, upper: bool = False) -> str:
    result = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr((65 if upper else 97) + remainder) + result
    return result or ("A" if upper else "a")


def _roman(value: int) -> str:
    numerals = (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    )
    result = ""
    for amount, symbol in numerals:
        while value >= amount:
            result += symbol
            value -= amount
    return result


def _formatted_number(value: int, number_format: str) -> str:
    if number_format == "lowerLetter":
        return _alpha(value)
    if number_format == "upperLetter":
        return _alpha(value, upper=True)
    if number_format == "lowerRoman":
        return _roman(value).lower()
    if number_format == "upperRoman":
        return _roman(value)
    return str(value)


def read_docx_paragraphs(path: Path) -> list[dict[str, Any]]:
    """Return non-empty paragraphs with their Word numbering level and label."""
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        number_to_abstract, abstract_levels = _numbering_definitions(archive)

    counters: dict[str, dict[int, int]] = defaultdict(dict)
    paragraphs: list[dict[str, Any]] = []
    for source_number, paragraph in enumerate(
        (node for node in root.iter() if _local(node.tag) == "p"), start=1
    ):
        text = re.sub(
            r"\s+", " ",
            "".join(
                node.text or ""
                for node in paragraph.iter()
                if _local(node.tag) == "t"
            ),
        ).strip()
        if not text:
            continue
        style = ""
        number_id = ""
        list_level: int | None = None
        for node in paragraph.iter():
            name = _local(node.tag)
            if name == "pStyle":
                style = _attribute(node, "val")
            elif name == "numId":
                number_id = _attribute(node, "val")
            elif name == "ilvl":
                try:
                    list_level = int(_attribute(node, "val", "0"))
                except ValueError:
                    list_level = 0
        number_label = ""
        if number_id:
            list_level = list_level if list_level is not None else 0
            state = counters[number_id]
            for deeper in [level for level in state if level > list_level]:
                state.pop(deeper, None)
            abstract_id = number_to_abstract.get(number_id, "")
            definition = abstract_levels.get((abstract_id, list_level), {})
            state[list_level] = state.get(
                list_level, int(definition.get("start", 1)) - 1
            ) + 1
            template = str(definition.get("template") or f"%{list_level + 1}.")
            for referenced_level in range(9):
                marker = f"%{referenced_level + 1}"
                if marker not in template:
                    continue
                ref_definition = abstract_levels.get(
                    (abstract_id, referenced_level), {}
                )
                value = state.get(
                    referenced_level, int(ref_definition.get("start", 1))
                )
                template = template.replace(
                    marker,
                    _formatted_number(
                        value, str(ref_definition.get("format") or "decimal")
                    ),
                )
            number_label = template
        paragraphs.append({
            "source_number": source_number,
            "text": text,
            "style": style,
            "num_id": number_id,
            "list_level": list_level,
            "number_label": number_label,
        })
    return paragraphs


def numbered_comment_groups(
    paragraphs: list[dict[str, Any]],
    start_index: int = 0,
) -> list[list[dict[str, Any]]]:
    """Group each level-zero list item with all following nested list items."""
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None
    for paragraph in paragraphs[start_index:]:
        if not paragraph.get("num_id"):
            current = None
            continue
        level = paragraph.get("list_level")
        if level in (None, 0) or current is None:
            current = [paragraph]
            groups.append(current)
        else:
            current.append(paragraph)
    return groups


def _record_text(record: dict[str, Any]) -> str:
    return re.sub(
        r"\s+", " ",
        str(record.get("verified_text") or record.get("original_text") or ""),
    ).strip()


def _row(record: dict[str, Any]) -> int | None:
    locator = record.get("source_locator_json")
    values = []
    if isinstance(locator, dict):
        values.extend([
            locator.get("xml_paragraph_index"),
            locator.get("paragraph_index"),
            locator.get("source_row"),
        ])
    values.append(record.get("source_row"))
    for value in values:
        try:
            if value not in (None, ""):
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _source_path(workspace: Path, source_document: str) -> Path:
    source = Path(source_document.split(" | ", 1)[0])
    return source if source.is_absolute() else workspace / source


def merge_docx_comment_hierarchy(
    dataset: dict[str, Any],
    workspace: Path,
    paragraph_loader: Callable[[Path], list[dict[str, Any]]] = read_docx_paragraphs,
) -> dict[str, Any]:
    """Merge stored DOCX subpoint records into their level-zero parent.

    Child rows remain immutable audit records, but are excluded from production
    search/counts. A response is inherited only when the entire hierarchy has
    zero or one distinct response ID.
    """
    comments = dataset.get("comments", [])
    links = {
        str(row.get("comment_id", "")): row
        for row in dataset.get("comment_response_links", [])
    }
    comments_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in comments:
        source = str(record.get("source_document", "")).split(" | ", 1)[0]
        if (
            source.casefold().endswith(".docx")
            and record.get("extraction_method") == "docx_numbered_paragraph"
            and _row(record) is not None
        ):
            comments_by_source[source].append(record)

    report: dict[str, Any] = {
        "hierarchy_groups_merged": 0,
        "hierarchy_children_suppressed": 0,
        "hierarchy_conflicts": [],
    }
    for source, records in comments_by_source.items():
        path = _source_path(workspace, source)
        if not path.is_file():
            report["hierarchy_conflicts"].append(f"{source}: source file is missing")
            continue
        try:
            paragraphs = paragraph_loader(path)
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            report["hierarchy_conflicts"].append(f"{source}: {exc}")
            continue
        record_by_row = {_row(record): record for record in records}
        for paragraph_group in numbered_comment_groups(paragraphs):
            stored = [
                (paragraph, record_by_row.get(int(paragraph["source_number"])))
                for paragraph in paragraph_group
            ]
            members = [(paragraph, record) for paragraph, record in stored if record]
            if len(members) < 2 or int(members[0][0].get("list_level") or 0) != 0:
                continue
            parent_paragraph, parent = members[0]
            children = [
                (paragraph, record)
                for paragraph, record in members[1:]
                if int(paragraph.get("list_level") or 0) > 0
            ]
            if not children:
                continue
            response_ids = {
                str(record.get("response_id") or links.get(
                    str(record.get("comment_id", "")), {}
                ).get("response_id") or "")
                for _, record in members
            } - {""}
            if len(response_ids) > 1:
                ids = ", ".join(sorted(
                    str(record.get("comment_id", "")) for _, record in members
                ))
                report["hierarchy_conflicts"].append(
                    f"{source}: conflicting responses in hierarchy {ids}"
                )
                continue

            existing_components = {
                str(component.get("comment_id", "")): component
                for component in parent.get("hierarchy_components", [])
                if isinstance(component, dict)
            }

            def component_text(record: dict[str, Any]) -> str:
                existing = existing_components.get(str(record.get("comment_id", "")), {})
                value = existing.get("verified_text") or existing.get("original_text")
                return re.sub(r"\s+", " ", str(value)).strip() if value else _record_text(record)

            component_rows = []
            full_parts = [component_text(parent)]
            for paragraph, record in children:
                label = str(paragraph.get("number_label") or "").strip()
                text = component_text(record)
                full_parts.append(f"{label} {text}".strip())
            for paragraph, record in members:
                existing = existing_components.get(str(record.get("comment_id", "")), {})
                component_rows.append({
                    "comment_id": record.get("comment_id", ""),
                    "label": paragraph.get("number_label", ""),
                    "list_level": paragraph.get("list_level"),
                    "source_row": _row(record),
                    "original_text": existing.get(
                        "original_text", record.get("original_text", "")
                    ),
                    "verified_text": existing.get(
                        "verified_text", record.get("verified_text", "")
                    ),
                })

            parent.setdefault("raw_original_text", parent.get("original_text", ""))
            parent["verified_text"] = "\n".join(part for part in full_parts if part)
            parent["text_trust_status"] = "verified"
            parent["search_eligible"] = True
            parent["hierarchy_status"] = "merged_parent"
            parent["hierarchy_components"] = component_rows
            parent["merged_child_comment_ids"] = [
                str(record.get("comment_id", "")) for _, record in children
            ]
            start_row = int(parent_paragraph["source_number"])
            end_row = max(int(paragraph["source_number"]) for paragraph, _ in members)
            parent["source_row"] = start_row
            parent["source_row_end"] = end_row
            parent["source_location"] = f"paragraphs {start_row}-{end_row}"
            locator = copy.deepcopy(
                parent.get("source_locator_json")
                if isinstance(parent.get("source_locator_json"), dict)
                else {}
            )
            locator.update({
                "paragraph_index": start_row,
                "paragraph_index_end": end_row,
                "paragraph_indices": [
                    int(paragraph["source_number"]) for paragraph, _ in members
                ],
                "match_method": "docx_numbering_hierarchy",
                "exact_quote": parent["verified_text"],
            })
            parent["source_locator_json"] = locator

            parent_id = str(parent.get("comment_id", ""))
            if response_ids and not parent.get("response_id"):
                response_id = next(iter(response_ids))
                parent["response_id"] = response_id
                parent["match_status"] = "matched"
                source_link = next(
                    (
                        links.get(str(record.get("comment_id", "")), {})
                        for _, record in members
                        if str(record.get("response_id") or links.get(
                            str(record.get("comment_id", "")), {}
                        ).get("response_id") or "") == response_id
                    ),
                    {},
                )
                if parent_id in links and source_link:
                    preserved_comment_id = links[parent_id].get("comment_id", parent_id)
                    links[parent_id].update(copy.deepcopy(source_link))
                    links[parent_id]["comment_id"] = preserved_comment_id
            if parent_id in links:
                links[parent_id]["hierarchy_component_comment_ids"] = [
                    str(record.get("comment_id", "")) for _, record in members
                ]
                links[parent_id]["comment_locator_json"] = copy.deepcopy(locator)

            for _, child in children:
                child_id = str(child.get("comment_id", ""))
                child["search_eligible"] = False
                child["duplicate_of"] = parent_id
                child["duplicate_status"] = "hierarchical_subpoint"
                child["hierarchy_parent_id"] = parent_id
                if child_id in links:
                    links[child_id]["hierarchy_parent_id"] = parent_id
            report["hierarchy_groups_merged"] += 1
            report["hierarchy_children_suppressed"] += len(children)
    return report


def refresh_hierarchy_source_locations(
    dataset: dict[str, Any],
    registry: dict[str, Any],
) -> int:
    """Refresh only hierarchy-affected source entries in an existing registry."""
    parents = {
        str(record.get("comment_id", "")): record
        for record in dataset.get("comments", [])
        if record.get("hierarchy_status") == "merged_parent"
    }
    updated = 0
    for source in registry.get("sources", {}).values():
        if source.get("relation") != "Primary source":
            continue
        record = parents.get(str(source.get("owner_id", "")))
        if not record:
            continue
        location = source.setdefault("location", {})
        text = _record_text(record)
        locator = (
            record.get("source_locator_json")
            if isinstance(record.get("source_locator_json"), dict)
            else {}
        )
        start = locator.get("paragraph_index") or record.get("source_row")
        end = locator.get("paragraph_index_end") or record.get("source_row_end") or start
        location.update({
            "exact_quote": text,
            "normalized_quote": re.sub(r"\s+", " ", text).strip().casefold(),
            "paragraph_index": int(start) if str(start).isdigit() else None,
            "paragraph_index_end": int(end) if str(end).isdigit() else None,
        })
        metadata = location.setdefault("metadata", {})
        metadata.update({
            "hierarchy_status": "merged_parent",
            "paragraph_indices": locator.get("paragraph_indices", []),
            "legacy_location": record.get("source_location", ""),
        })
        updated += 1
    return updated
