import json
import tempfile
import unittest
from pathlib import Path

from phase2.backfill_timeline_dates import backfill, filename_date


class TimelineDateBackfillTests(unittest.TestCase):
    def test_filename_timestamp_is_reduced_to_calendar_date(self):
        self.assertEqual(
            filename_date("Response_PC3-Plans20260114132207[1].pdf"),
            ("2026-01-14", "20260114132207", "filename_compact_timestamp"),
        )

    def test_filename_date_does_not_turn_clock_suffix_into_date(self):
        self.assertEqual(
            filename_date("2025-100917 CI_02-20-2026_5_04_PM.xlsx"),
            ("2026-02-20", "02-20-2026", "filename_numeric"),
        )

    def test_backfill_replaces_legacy_clock_suffix_date(self):
        dataset = {
            "comments": [{
                "comment_id": "C1",
                "source_document": "new/site/CI_02-20-2026_5_04_PM.xlsx",
                "source_document_date": "2026-05-04",
                "source_date_evidence": "2026_5_04",
                "source_date_method": "filename_iso",
                "document_date_iso": "2026-05-04",
                "document_date_source": "filename_iso",
                "document_date": {"iso": "2026-05-04", "source": "filename_iso"},
            }],
            "responses": [], "comment_response_links": [],
            "issue_event_index": {}, "source_files": {}, "sources": [],
            "canonical_documents": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            backfill(path, Path(directory))
            result = json.loads(path.read_text(encoding="utf-8"))
        row = result["comments"][0]
        self.assertEqual(row["source_document_date"], "2026-02-20")
        self.assertEqual(row["document_date_iso"], "2026-02-20")

    def test_reviewer_header_date_is_event_date_not_document_date(self):
        dataset = {
            "comments": [{
                "comment_id": "C1",
                "reviewer": "Building Review Jane Doe 7/11/25 3:50 PM",
                "source_document": "new/site/CI_02-20-2026_5_04_PM.xlsx",
            }],
            "responses": [], "comment_response_links": [],
            "issue_event_index": {}, "source_files": {}, "sources": [],
            "canonical_documents": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            backfill(path, Path(directory))
            result = json.loads(path.read_text(encoding="utf-8"))
        row = result["comments"][0]
        self.assertEqual(row["event_date"], "2025-07-11")
        self.assertEqual(row["event_date_source"], "reviewer_header")
        self.assertEqual(row["source_document_date"], "2026-02-20")

    def test_backfill_propagates_date_to_events_and_response_status(self):
        dataset = {
            "comments": [{
                "comment_id": "C1",
                "source_document": "new/site/Response_PC3-Plans20260114132207[1].pdf",
                "source_document_date": "",
                "source_date_method": "missing",
                "issue_thread_events": [],
            }],
            "responses": [{
                "comment_id": "C1",
                "response_id": "R1",
                "source_document": "new/site/Response_PC3-Plans20260114132207[1].pdf",
                "response_date_iso": "2026-01-20",
            }],
            "comment_response_links": [{
                "comment_id": "C1",
                "source_document": "new/site/Response_PC3-Plans20260114132207[1].pdf",
            }],
            "issue_event_index": {
                "T1": {"events": [{
                    "event_type": "government_comment",
                    "exact_text": "Provide the detail.",
                    "source_occurrences": [{
                        "comment_id": "C1",
                        "source_document": "new/site/Response_PC3-Plans20260114132207[1].pdf",
                    }],
                }]}
            },
            "source_files": {},
            "sources": [],
            "canonical_documents": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            report = backfill(path, Path(directory))
            result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["gemini_calls"], 0)
        self.assertEqual(result["comments"][0]["source_document_date"], "2026-01-14")
        self.assertEqual(result["responses"][0]["source_document_date"], "2026-01-14")
        self.assertEqual(result["responses"][0]["event_date"], "2026-01-20")
        self.assertEqual(
            result["issue_event_index"]["T1"]["events"][0]["source_document_date"],
            "2026-01-14",
        )

    def test_existing_confirmed_date_wins_over_filename(self):
        dataset = {
            "comments": [{
                "comment_id": "C1",
                "source_document": "new/site/20260114132207.pdf",
                "source_document_date": "2025-12-01",
            }],
            "responses": [], "comment_response_links": [],
            "issue_event_index": {}, "source_files": {},
            "sources": [], "canonical_documents": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            backfill(path, Path(directory))
            result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["comments"][0]["source_document_date"], "2025-12-01")


if __name__ == "__main__":
    unittest.main()
