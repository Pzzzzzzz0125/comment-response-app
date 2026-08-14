"""Low-overhead, content-free telemetry for Gemini ingestion requests."""

from __future__ import annotations

import json
import math
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TRACE_SCHEMA_VERSION = "gemini-straggler-v1"


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def append_trace_event(path: Path | None, event: dict[str, Any]) -> None:
    """Append one compact JSON event without logging prompts or source text."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"schema_version": TRACE_SCHEMA_VERSION, **event},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def finished_request_events(path: Path | None) -> list[dict[str, Any]]:
    """Read terminal request events from an append-only trace, best effort."""
    if path is None or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("event") == "request_finished"
                ):
                    rows.append(event)
    except OSError:
        return []
    return rows


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read PNG/JPEG dimensions with the standard library, best effort."""
    try:
        with path.open("rb") as stream:
            header = stream.read(24)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                width, height = struct.unpack(">II", header[16:24])
                return int(width), int(height)
            if not header.startswith(b"\xff\xd8"):
                return None, None
            stream.seek(2)
            while True:
                marker_start = stream.read(1)
                if not marker_start:
                    return None, None
                if marker_start != b"\xff":
                    continue
                marker = stream.read(1)
                while marker == b"\xff":
                    marker = stream.read(1)
                if not marker or marker in {b"\xd8", b"\xd9"}:
                    continue
                length_raw = stream.read(2)
                if len(length_raw) != 2:
                    return None, None
                length = int.from_bytes(length_raw, "big")
                if length < 2:
                    return None, None
                if marker[0] in {
                    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
                }:
                    dimensions = stream.read(5)
                    if len(dimensions) != 5:
                        return None, None
                    height = int.from_bytes(dimensions[1:3], "big")
                    width = int.from_bytes(dimensions[3:5], "big")
                    return width, height
                stream.seek(length - 2, 1)
    except OSError:
        return None, None


def percentile(values: Iterable[float], percentage: float) -> float | None:
    samples = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not samples:
        return None
    if len(samples) == 1:
        return round(samples[0], 4)
    position = (len(samples) - 1) * percentage
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(samples[lower], 4)
    value = samples[lower] + (samples[upper] - samples[lower]) * (position - lower)
    return round(value, 4)


def _latency_summary(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [
        float(row[field])
        for row in rows
        if isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
    ]
    return {
        "samples": len(values),
        "p50_seconds": percentile(values, 0.50),
        "p95_seconds": percentile(values, 0.95),
        "max_seconds": round(max(values), 4) if values else None,
    }


def summarize_request_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize stragglers without changing request scheduling or timeouts."""
    completed = [row for row in rows if row.get("status") == "completed"]
    classified: list[dict[str, Any]] = []
    for row in rows:
        reasons: list[str] = []
        first = row.get("time_to_first_token")
        generation = row.get("generation_duration")
        elapsed = row.get("elapsed_seconds")
        if isinstance(first, (int, float)) and first >= 90:
            reasons.append("long_time_to_first_token")
        if isinstance(generation, (int, float)) and generation >= 120:
            reasons.append("long_generation")
        if int(row.get("retry_count") or 0) > 0:
            reasons.append("retried")
        if row.get("status") == "status_unknown":
            reasons.append("status_unknown")
        if isinstance(elapsed, (int, float)) and elapsed >= 180 and not reasons:
            reasons.append("long_total_latency")
        if reasons:
            classified.append({
                "request_id": row.get("request_id", ""),
                "stage": row.get("stage", ""),
                "page_numbers": row.get("page_numbers", []),
                "elapsed_seconds": elapsed,
                "reasons": reasons,
            })
    slowest = sorted(
        rows,
        key=lambda row: float(row.get("elapsed_seconds") or 0.0),
        reverse=True,
    )[:5]
    by_stage: dict[str, Any] = {}
    for stage in sorted({str(row.get("stage") or "unknown") for row in rows}):
        stage_rows = [
            row for row in completed
            if str(row.get("stage") or "unknown") == stage
        ]
        by_stage[stage] = {
            "request_count": len(stage_rows),
            "total": _latency_summary(stage_rows, "elapsed_seconds"),
            "time_to_first_token": _latency_summary(
                stage_rows, "time_to_first_token"
            ),
            "generation": _latency_summary(
                stage_rows, "generation_duration"
            ),
        }
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "request_count": len(rows),
        "completed_count": len(completed),
        "status_unknown_count": sum(
            row.get("status") == "status_unknown" for row in rows
        ),
        "retry_count": sum(int(row.get("retry_count") or 0) for row in rows),
        "latency": {
            "total": _latency_summary(completed, "elapsed_seconds"),
            "time_to_first_token": _latency_summary(
                completed, "time_to_first_token"
            ),
            "generation": _latency_summary(completed, "generation_duration"),
            "files_api_upload": _latency_summary(completed, "upload_duration"),
        },
        "by_stage": by_stage,
        "stragglers": classified,
        "slowest_requests": [{
            "request_id": row.get("request_id", ""),
            "stage": row.get("stage", ""),
            "page_numbers": row.get("page_numbers", []),
            "elapsed_seconds": row.get("elapsed_seconds"),
            "time_to_first_token": row.get("time_to_first_token"),
            "generation_duration": row.get("generation_duration"),
            "retry_count": row.get("retry_count", 0),
            "finish_reason": row.get("finish_reason", ""),
        } for row in slowest],
    }
