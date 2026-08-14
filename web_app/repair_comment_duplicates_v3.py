#!/usr/bin/env python3
"""Reapply only comment-level deduplication with the shared normalization_v3.

This intentionally avoids re-parsing documents or rebuilding the source
registry. It is the cheap, deterministic half of the historical repetition
repair: raw comments remain in place, duplicate rows are marked in place, and
their source occurrences are attached to the surviving row.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
from pathlib import Path
import sys

WORKSPACE_IMPORT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_IMPORT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_IMPORT))

from web_app.canonical_event import NORMALIZATION_VERSION
from web_app.comment_dedup import mark_duplicate_comments


def atomic_json(path: Path, value: object) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=f"{path.stem}-", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = mark_duplicate_comments(dataset)
    report.update({
        "normalization_version": NORMALIZATION_VERSION,
        "applied": args.apply,
    })
    if args.apply:
        backup = args.dataset.with_name(
            f"{args.dataset.stem}.pre-comment-dedup-v3-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
        )
        atomic_json(backup, json.loads(args.dataset.read_text(encoding="utf-8")))
        audit = dataset.setdefault("metadata", {}).setdefault("global_duplicate_audit", {})
        audit.update({
            "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "method": "normalization_v3_site_round_date_parameters_negations_response_event_date",
            "normalization_version": NORMALIZATION_VERSION,
            "duplicate_rows_suppressed": report.get("duplicate_rows_suppressed", 0),
            "duplicate_groups": report.get("duplicate_groups", 0),
            "source_occurrences_attached": report.get("source_occurrences_attached", 0),
            "response_occurrences_attached": report.get("response_occurrences_attached", 0),
            "duplicate_response_rows_suppressed": report.get("duplicate_response_rows_suppressed", 0),
            "duplicate_response_groups": report.get("duplicate_response_groups", 0),
            "response_source_occurrences_attached": report.get("response_source_occurrences_attached", 0),
        })
        atomic_json(args.dataset, dataset)
        report["backup_created"] = str(backup)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
