#!/usr/bin/env python3
"""Cross-platform local launcher for Permit Precedents.

The launcher performs a small preflight, creates ``.env.local`` from the
checked-in template when needed, and starts the existing Python server.  It
does not copy, download, or modify permit data.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
ENV_LOCAL = ROOT / ".env.local"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if key.strip() and value:
            values[key.strip()] = value
    return values


def configured_path(values: dict[str, str], key: str, default: Path) -> Path:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


def port_is_busy(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.25):
            return True
    except OSError:
        return False


def ensure_local_env() -> bool:
    if ENV_LOCAL.exists():
        return False
    if not ENV_EXAMPLE.is_file():
        raise SystemExit("Missing .env.example; restore it from Git first.")
    shutil.copyfile(ENV_EXAMPLE, ENV_LOCAL)
    print("Created .env.local from .env.example.")
    print("Add GEMINI_API_KEY and ADOBE_PDF_EMBED_CLIENT_ID when needed.\n")
    return True


def build_demo_arguments() -> list[str]:
    return [
        "--dataset", str(ROOT / "demo_data" / "dataset.json"),
        "--source-root", str(ROOT / "demo_sources"),
        "--categories", str(ROOT / "demo_data" / "category_assignments.json"),
        "--source-registry", str(ROOT / "demo_data" / "source_registry.json"),
        "--preview-root", str(ROOT / "demo_data" / "previews"),
        "--enrichment", str(ROOT / "demo_data" / "gemini_enrichment.json"),
        "--search-index", str(ROOT / "demo_data" / "search_index.json"),
        "--link-reviews", str(ROOT / "demo_data" / "link_review_decisions.json"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use the checked-in synthetic dataset instead of private permit data.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run the preflight without starting the server.",
    )
    args = parser.parse_args()

    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")

    created_env = ensure_local_env()
    values = read_env_file(ENV_LOCAL)
    # Match the server's precedence: operating-system variables override the
    # developer-local file. This also makes CI and PowerShell configuration
    # behave exactly like direct server startup.
    values.update({key: value for key, value in os.environ.items() if value})
    host = values.get("PERMIT_HOST", "127.0.0.1")
    try:
        port = int(values.get("PERMIT_PORT", "8010"))
    except ValueError as exc:
        raise SystemExit("PERMIT_PORT in .env.local must be a number.") from exc

    static_index = configured_path(
        values, "PERMIT_STATIC_ROOT", ROOT / "web_app" / "static"
    ) / "index.html"
    if not static_index.is_file():
        raise SystemExit(
            "Frontend build is missing. Run `npm ci` and `npm run build` "
            "inside the frontend directory."
        )

    server_args: list[str] = []
    mode = "synthetic demo" if args.demo else "authorized local dataset"
    if args.demo:
        server_args.extend(build_demo_arguments())
        dataset = ROOT / "demo_data" / "dataset.json"
        source_root = ROOT / "demo_sources"
    else:
        dataset = configured_path(
            values, "PERMIT_DATASET_PATH", ROOT / "phase2_dataset" / "dataset.json"
        )
        source_root = configured_path(
            values, "PERMIT_SOURCE_ROOT", ROOT / "comments&response"
        )
        if not dataset.is_file():
            print("Authorized local data is incomplete:")
            print(f"  - dataset: {dataset}")
            print("\nAsk the project owner for the private data bundle, or run:")
            print(f"  {Path(sys.executable).name} scripts/run_local.py --demo")
            return 2
        if not source_root.is_dir():
            print("Original source folder is not installed:")
            print(f"  - {source_root}")
            print("Library, search, timelines, and AI retrieval remain available.")
            print("Original-file viewing and citation highlighting will be unavailable.\n")

    print(f"Python: {sys.version.split()[0]}")
    print(f"Mode: {mode}")
    print(f"Dataset: {dataset}")
    print(f"Sources: {source_root}")
    print(f"App URL: http://{host}:{port}")
    if not values.get("GEMINI_API_KEY") and not values.get("GOOGLE_API_KEY"):
        print("Note: Gemini features remain disabled until a key is added.")
    if created_env:
        print("Review .env.local before sharing or enabling AI features.")

    if args.check:
        print("Local preflight passed.")
        return 0
    if port_is_busy(host, port):
        raise SystemExit(
            f"Port {port} is already in use. Stop the existing app or change "
            "PERMIT_PORT in .env.local."
        )

    command = [sys.executable, str(ROOT / "web_app" / "server.py"), *server_args]
    try:
        return subprocess.call(command, cwd=ROOT)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
