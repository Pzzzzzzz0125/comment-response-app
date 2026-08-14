import json
from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from phase2.straggler_monitor import (
    finished_request_events,
    summarize_request_metrics,
)
from phase2.visual_ingestion import (
    RequestStatusUnknownError,
    VisualGeminiClient,
)


class FakeStreamResponse:
    def __init__(self, lines=None, error=None):
        self.lines = iter(lines or [])
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self):
        if self.error is not None:
            raise self.error
        return next(self.lines, b"")


def sse_event(payload):
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


class StragglerMonitorTests(unittest.TestCase):
    def test_streamed_structured_output_records_timing_and_counts(self):
        first = {
            "candidates": [{
                "content": {"parts": [{"text": '{"records":['}]},
            }],
        }
        second = {
            "candidates": [{
                "content": {"parts": [{"text": '{"id":1}]}' }]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 11,
                "cachedContentTokenCount": 3,
                "candidatesTokenCount": 7,
            },
        }
        response = FakeStreamResponse(
            sse_event(first).splitlines(keepends=True)
            + sse_event(second).splitlines(keepends=True)
            + [b""]
        )
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "straggler_trace.jsonl"
            client = VisualGeminiClient("test-key")
            client._trace_path = trace
            client._pending_request_context = {
                "stage": "gemini_extraction",
                "page_numbers": [4, 5],
                "image_count": 2,
                "image_resolution": [],
                "evidence_unit_count": 2,
                "expected_record_count": 1,
                "request_created_at": "2026-07-31T00:00:00.000Z",
            }
            with patch(
                "phase2.visual_ingestion.urlopen", return_value=response,
            ) as mocked:
                result = client._request(
                    "extract", [{"text": "evidence"}],
                    {"type": "OBJECT"}, stage="gemini_extraction",
                )

            self.assertEqual(result, {"records": [{"id": 1}]})
            request_url = mocked.call_args.args[0].full_url
            self.assertIn(":streamGenerateContent?alt=sse", request_url)
            metadata = client.last_request_metadata
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["finish_reason"], "STOP")
            self.assertEqual(metadata["retry_count"], 0)
            self.assertEqual(metadata["actual_record_count"], 1)
            self.assertEqual(metadata["input_tokens"], 11)
            self.assertEqual(metadata["cached_input_tokens"], 3)
            self.assertEqual(metadata["output_tokens"], 7)
            self.assertIsNotNone(metadata["time_to_first_token"])
            self.assertIsNotNone(metadata["generation_duration"])
            events = [
                json.loads(line) for line in trace.read_text().splitlines()
            ]
            self.assertEqual(
                [event["event"] for event in events],
                ["request_created", "attempt_submitted", "request_finished"],
            )
            self.assertEqual(len(finished_request_events(trace)), 1)

    def test_unknown_request_status_is_not_automatically_retried(self):
        response = FakeStreamResponse(error=TimeoutError("read timed out"))
        client = VisualGeminiClient("test-key")
        with patch(
            "phase2.visual_ingestion.urlopen", return_value=response,
        ) as mocked:
            with self.assertRaises(RequestStatusUnknownError):
                client._request(
                    "extract", [{"text": "evidence"}],
                    {"type": "OBJECT"}, stage="gemini_extraction",
                )
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(client.last_request_metadata["status"], "status_unknown")
        self.assertEqual(client.last_request_metadata["retry_count"], 0)

    def test_high_demand_503_is_bounded_to_one_retry(self):
        def high_demand_error():
            return HTTPError(
                "https://example.invalid", 503, "Unavailable", {},
                BytesIO(b'{"error":{"message":"high demand"}}'),
            )

        client = VisualGeminiClient("test-key")
        errors = [high_demand_error(), high_demand_error()]
        try:
            with patch(
                "phase2.visual_ingestion.urlopen", side_effect=errors,
            ) as mocked, patch("phase2.visual_ingestion.time.sleep"):
                with self.assertRaises(RuntimeError):
                    client._request(
                        "extract", [{"text": "evidence"}],
                        {"type": "OBJECT"}, stage="gemini_extraction",
                    )
        finally:
            for error in errors:
                error.close()
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(client.last_request_metadata["retry_count"], 1)
        self.assertEqual(client.last_request_metadata["status"], "failed_definitive")

    def test_summary_reports_percentiles_and_straggler_causes(self):
        summary = summarize_request_metrics([
            {
                "request_id": "fast", "stage": "gemini_extraction",
                "status": "completed", "elapsed_seconds": 10,
                "time_to_first_token": 2, "generation_duration": 8,
                "upload_duration": 0, "retry_count": 0,
            },
            {
                "request_id": "slow", "stage": "gemini_extraction",
                "status": "completed", "elapsed_seconds": 260,
                "time_to_first_token": 100, "generation_duration": 160,
                "upload_duration": 0, "retry_count": 1,
            },
        ])
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["latency"]["total"]["p50_seconds"], 135.0)
        self.assertEqual(summary["retry_count"], 1)
        self.assertEqual(summary["stragglers"][0]["request_id"], "slow")
        self.assertEqual(
            set(summary["stragglers"][0]["reasons"]),
            {"long_time_to_first_token", "long_generation", "retried"},
        )


if __name__ == "__main__":
    unittest.main()
