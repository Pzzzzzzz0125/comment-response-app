#!/usr/bin/env python3
"""Repair the cumulative Menlo Park PC3 #141 fire-separation timeline.

The source PDF is an accumulated review form. Its PC3 row contains the
original requirement followed by inline PC2 and PC3 follow-ups. The raw row
stays immutable; this script only adds a reviewed display projection and
removes the duplicate indexed copy of the opening requirement.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


TARGET_IDS = {"C-04524b4a43231310", "C-434836bba6bf8e5c"}
THREAD_ID = "T-e083c66e9777e859"

FIRE_SEPARATION_PC3_DISPLAY = """PC3 · #141 Continue
iv. Floor/ceiling assemblies that separate dwelling units shall be of one-hour fire resistance rated assembly that has been tested in accordance with ASTM E119 or UL 263 or section 703.3 of the CBC.
2. Call out the detail on the section view.

v. Supporting Construction:
1. Provide floor framing plan showing location of the first floor bearing walls that supports the second floor/ceiling system.

vi. Joint detail:
1. Where 1 hour walls butt up against the 1 hour floor ceiling.
2. At the joint between the ground floor and the 1 hour walls.

4. Shower/tub drain assemblies per R302.4.1.1.
5. Water closet floor flange penetrations."""


def atomic_json(path: Path, payload: Any) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f"{path.stem}-", suffix=".tmp", delete=False
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def backup(path: Path, suffix: str) -> None:
    destination = path.with_name(path.name + suffix)
    if not destination.exists():
        shutil.copy2(path, destination)


def clean_opening_requirement(text: str) -> str:
    """Keep the current PC3 requirement and remove inline historical rounds."""
    value = str(text or "").replace("\r", "")
    # The source starts with ``(A) PC3- #141 Continue``. It is a display
    # marker, not part of the requirement itself.
    value = value.replace("(A) PC3- #141 Continue", "PC3 · #141 Continue", 1)
    value = value.replace("(A) PC3- #141 Continue", "PC3 · #141 Continue", 1)
    # The PC3 continuation row has a stable, structured set of headings. The
    # inline PC2/PC3 clauses are represented by separate indexed events; keep
    # only the base requirement headings here so the opening event is concise
    # without losing any requirement text.
    if "Floor/ceiling assemblies that separate dwelling units" in value and "Supporting Construction:" in value:
        return FIRE_SEPARATION_PC3_DISPLAY
    # Conservative fallback for a future export with the same prefix but a
    # different body: remove only the first inline historical marker.
    for marker in ("\nPC2:", " PC2:"):
        if marker in value:
            value = value.split(marker, 1)[0]
            break
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip()


def repair(dataset_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    changed: list[str] = []
    for comment in dataset.get("comments", []):
        if not isinstance(comment, dict) or comment.get("comment_id") not in TARGET_IDS:
            continue
        raw = str(comment.get("verified_text") or comment.get("original_text") or "")
        cleaned = clean_opening_requirement(raw)
        if not cleaned or cleaned == raw:
            continue
        comment["display_text_override"] = cleaned
        comment["timeline_comment_text"] = cleaned
        comment["display_label_override"] = "PC3 · #141 Continue"
        comment.setdefault("repair_history", []).append({
            "repair": "cumulative_pc3_comment_display_split",
            "removed_inline_rounds": ["PC2", "PC3"],
            "raw_text_preserved": True,
            "display_text": cleaned,
        })
        changed.append(str(comment["comment_id"]))

    removed_events: list[str] = []
    thread = dataset.get("issue_event_index", {}).get(THREAD_ID)
    if isinstance(thread, dict):
        events = thread.get("events", [])
        kept: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            text = str(event.get("exact_text") or event.get("text") or "")
            # E-897... is the indexed copy of the opening PC3 requirement;
            # the displayed government event above now owns that text.
            duplicate_opening = (
                str(event.get("event_id", "")) == "E-897c972d8c4146098bce"
                or (
                    str(event.get("event_type", "")) == "reviewer_follow_up"
                    and str(event.get("effective_round") or event.get("review_round")) in {"3", "PC3"}
                    and "#141 Continue" in text
                    and "PC2:" not in text
                )
            )
            if duplicate_opening:
                removed_events.append(str(event.get("event_id", "")))
                continue
            kept.append(event)
        thread["events"] = kept
        thread["canonical_event_count"] = len(kept)
        thread["raw_event_count"] = len(kept)
        thread["duplicate_event_count"] = int(thread.get("duplicate_event_count", 0) or 0) + len(removed_events)

    report = {
        "changed_comments": changed,
        "removed_duplicate_index_events": removed_events,
        "raw_text_preserved": True,
        "dry_run": dry_run,
    }
    if not dry_run:
        backup(dataset_path, ".pre_fire_separation_timeline_repair.json")
        dataset.setdefault("metadata", {})["fire_separation_timeline_repair"] = report
        atomic_json(dataset_path, dataset)
    return report


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair(args.dataset, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
