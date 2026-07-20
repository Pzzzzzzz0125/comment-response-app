#!/usr/bin/env python3
"""Build or update the opaque source/document registry and office previews."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from source_registry import SourceRegistry


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--source-root", type=Path, default=workspace / "comments&response")
    parser.add_argument("--registry", type=Path, default=workspace / "web_app" / "data" / "source_registry.json")
    parser.add_argument("--preview-root", type=Path, default=workspace / "web_app" / "data" / "previews")
    args = parser.parse_args()
    registry = SourceRegistry(
        args.dataset, args.source_root, args.registry, args.preview_root, auto_migrate=False,
    )
    print(json.dumps(registry.migrate(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
