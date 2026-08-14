"""Small, reviewable Common Topic taxonomy.

Common Topic is an aspect classification, not a similarity/deduplication
operation.  The rules are deliberately conservative and return stable topic
ids; a future Gemini classifier can replace the rule body without changing the
issue/event/source data model.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


TOPIC_TAXONOMY_VERSION = "taxonomy_v1"


def _text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def classify_topic(text: Any, discipline: Any = "") -> dict[str, str]:
    body = _text(text)
    value = _text(f"{discipline} {text}")

    if re.search(r"\b(tree|trees|arbor|arborist|heritage tree)\b", value):
        if re.search(r"\b(arborist|report|monitor|inspection|documentation|submit)\b", body):
            aspect = "arborist documentation"
        elif re.search(r"\b(protect|protection|fenc|root|excavat|impact|prun|preserv)\b", body):
            aspect = "tree protection and impact mitigation"
        elif re.search(r"\b(remov|circumference|inventory|tree id|label|size|classification)\b", body):
            aspect = "tree removal and inventory"
        else:
            aspect = "tree review"
        return {"topic_id": f"TREES_{_slug(aspect)}", "parent": "Trees", "aspect": aspect}

    if re.search(r"\b(door|doors|opening)\b", value):
        if re.search(r"\b(fire[- ]?rated|fire[- ]?resistance|assembly|smoke)\b", body):
            aspect = "door fire rating"
        elif re.search(r"\b(swing|egress|exit|landing|clearance)\b", body):
            aspect = "door swing and egress"
        elif re.search(r"\b(width|height|dimension|size|clear|\d+['\"]|minimum)\b", body):
            aspect = "door dimensions and clear width"
        elif re.search(r"\b(hardware|hinge|latch|handle|closer)\b", body):
            aspect = "door hardware"
        else:
            aspect = "door schedule and labeling"
        return {"topic_id": f"DOORS_{_slug(aspect)}", "parent": "Doors", "aspect": aspect}

    rules = (
        ("DRAINAGE_STORMWATER", "Drainage", "drainage and stormwater", r"drain|stormwater|runoff|retention|infiltration|swale"),
        ("STRUCTURAL_CALCULATIONS", "Structural", "structural calculations and framing", r"structur|seismic|shear|beam|joist|foundation|hanger|ledger|load"),
        ("FIRE_SEPARATION", "Fire", "fire separation and rated assemblies", r"fire|rated|sprinkler|smoke|carbon monoxide|separation"),
        ("ACCESSIBILITY", "Accessibility", "accessibility and clearances", r"accessible|ada|accessibility|grab bar|clearance|reach range"),
        ("GRADING_SITE_WORK", "Site work", "grading and site work", r"grading|slope|retaining|site plan|earthwork"),
        ("SETBACKS_ZONING", "Zoning", "setbacks and zoning compliance", r"setback|zoning|lot coverage|floor area|height limit|property line"),
        ("ENERGY_COMPLIANCE", "Energy", "energy compliance", r"energy|calgreen|hers|insulation|resnet|title 24"),
        ("PARKING_ACCESS", "Planning", "parking and vehicle access", r"parking|driveway|garage|vehicle access|stall"),
    )
    for topic_id, parent, aspect, pattern in rules:
        if re.search(rf"\b(?:{pattern})\b", value):
            return {"topic_id": topic_id, "parent": parent, "aspect": aspect}

    parent = str(discipline or "General").strip().title() or "General"
    aspect = f"{parent.casefold()} review"
    return {"topic_id": f"GENERAL_{_slug(aspect)}", "parent": parent, "aspect": aspect}


def _slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
