import tempfile
import unittest
from pathlib import Path

from phase2.spreadsheet_ingestion import (
    build_spreadsheet_evidence,
    detect_spreadsheet_schemas,
    local_verification_result,
    parse_discussion_events,
)
from phase2.visual_ingestion import (
    EvidenceBundle,
    PageImage,
    VisualIngestionPipeline,
    raw_text_for_page_batch,
    results_to_dataset_rows,
)
from web_app.source_registry import SourceLocation


def projectdox_raw():
    headers = [
        ("A", "REF #"), ("B", "REVIEWED BY"), ("C", "TYPE"),
        ("D", "VIEW"), ("E", "ENTER YOUR COMMENT RESPONSE HERE"),
        ("F", "DISCUSSION"), ("G", "CYCLE"), ("H", "STATUS"),
    ]

    def row(number, values):
        return {
            "row_number": number,
            "cells": [
                {
                    "column": column,
                    "address": f"{column}{number}",
                    "value": value,
                }
                for column, value in values
            ],
        }

    return {
        "kind": "xlsx_cells",
        "sheets": [{
            "name": "Review Comments",
            "merged_ranges": [],
            "has_drawing_objects": False,
            "rows": [
                row(1, headers),
                row(2, [
                    ("A", "1"), ("B", "Building Review 1/2/2026"),
                    ("C", "Keep  source&nbsp;text exact."),
                    ("D", "open"), ("E", "Exact response."),
                    ("F", "Earlier discussion is not a response."),
                    ("G", "2"), ("H", "Unresolved"),
                ]),
                row(3, [
                    ("A", "1"), ("B", "Planning Review 1/2/2026"),
                    ("C", "A second physical row with the same printed ref."),
                    ("D", "open"), ("E", ""), ("F", ""),
                    ("G", "2"), ("H", "Unresolved"),
                ]),
            ],
        }],
    }


def compact_verification(evidence):
    ids = [
        group["group_id"]
        for group in evidence["packet"]["groups"]
    ]
    return {
        "document_verified": True,
        "template_verified": True,
        "every_candidate_assigned": True,
        "same_row_links_correct": True,
        "verified_group_ids": ids,
        "rejected_group_ids": [],
        "missing_unit_ids": [],
        "incorrect_groupings": [],
        "incorrect_links": [],
        "verification_summary": "Every direct cell group is correct",
    }


class SpreadsheetIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_projectdox_rows_are_extracted_once_with_exact_cells(self):
        raw = projectdox_raw()
        schemas = detect_spreadsheet_schemas(raw)
        self.assertEqual(
            schemas[0]["template_id"],
            "projectdox_review_comments_v1",
        )
        evidence = build_spreadsheet_evidence(
            raw,
            schemas,
            {
                "property_hint": "100 Main St",
                "city_hint": "Menlo Park",
                "review_round_hint": "1",
            },
        )
        records = evidence["extraction"]["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(
            records[0]["exact_comment_text"],
            "Keep  source&nbsp;text exact.",
        )
        self.assertEqual(
            records[0]["normalized_comment_text"],
            "Keep source text exact.",
        )
        self.assertEqual(
            records[0]["comment_location"]["cell_range"], "C2",
        )
        self.assertEqual(
            records[0]["response_location"]["cell_range"], "E2",
        )
        self.assertEqual(records[0]["review_round"], "2")
        self.assertEqual(records[0]["event_date_iso"], "2026-01-02")
        self.assertEqual(records[0]["event_date_raw"], "1/2/2026")
        self.assertEqual(
            records[0]["event_date_location"]["cell_range"], "B2",
        )
        self.assertEqual(
            records[0]["source_metadata"]["reviewer_cell"], "B2",
        )
        self.assertEqual(
            evidence["completeness"]["candidate_comment_count"], 2,
        )
        self.assertEqual(
            evidence["completeness"]["candidate_response_count"], 1,
        )

    def test_discussion_history_becomes_chronological_issue_events(self):
        text = (
            "Reviewer Response: Eric Morgan - 6/30/26 3:02 PM\n"
            "Not addressed. Use circumference, not DBH.\n"
            "----------------------------------------------------------\n"
            "Responded by: Weiran Jia - 5/25/26 3:05 PM\n"
            "Tree sizes were added to Sheet A1.01.\n"
        )
        events = parse_discussion_events(
            text,
            {
                "viewer_type": "spreadsheet",
                "sheet_name": "Review Comments",
                "cell_range": "F4",
                "row_number": 4,
            },
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "reviewer_follow_up")
        self.assertEqual(events[0]["actor"], "Eric Morgan")
        self.assertEqual(
            events[0]["exact_text"],
            "Not addressed. Use circumference, not DBH.",
        )
        self.assertEqual(events[1]["event_type"], "applicant_response")
        self.assertEqual(events[1]["occurred_at"], "2026-05-25T15:05")
        self.assertEqual(
            events[1]["source_location"]["cell_range"], "F4",
        )

    def test_flattened_discussion_is_split_into_compact_role_entries(self):
        text = (
            "Reviewer Response: Gregg Schwartz - 9/23/25 3:29 PM "
            "This comment to remain open until all other comments are resolved. "
            "---------------------------------------------------------- "
            "Responded by: Mau Pham - 9/11/25 5:54 PM noted "
            "---------------------------------------------------------- "
            "Reviewer Response: Gregg Schwartz - 8/6/25 9:18 AM "
            "Please see the original comment and respond clearly."
        )
        events = parse_discussion_events(text, {"cell_range": "F39"})
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["display_label"], "Reviewer follow-up")
        self.assertIn("remain open", events[0]["exact_text"])
        self.assertEqual(events[1]["display_label"], "Applicant response")
        self.assertEqual(events[1]["actor"], "Mau Pham")
        self.assertIn("Please see the original comment", events[2]["exact_text"])

    def test_compact_verification_translates_to_confirmed_dataset_rows(self):
        raw = projectdox_raw()
        evidence = build_spreadsheet_evidence(
            raw,
            detect_spreadsheet_schemas(raw),
            {"city_hint": "Menlo Park"},
        )
        verification = local_verification_result(
            evidence, compact_verification(evidence),
        )
        source = self.root / "comments.xlsx"
        source.write_bytes(b"xlsx")
        bundle = EvidenceBundle(
            "VI-sheet",
            source,
            "abc",
            "xlsx",
            raw,
            [],
            self.root,
        )
        comments, responses, links, summary, review = results_to_dataset_rows(
            bundle,
            evidence["extraction"],
            verification,
            "comments&response/comments.xlsx",
        )
        self.assertEqual(len(comments), 2)
        self.assertEqual(len(responses), 1)
        self.assertTrue(all(row["search_eligible"] for row in comments))
        self.assertEqual(comments[0]["source_sheet"], "Review Comments")
        self.assertEqual(comments[0]["source_row"], 2)
        self.assertEqual(
            comments[0]["source_location"],
            "sheet Review Comments · cell C2 · government comment cell",
        )
        self.assertEqual(
            links[0]["matching_method"], "same_visible_row_structured",
        )
        self.assertEqual(
            comments[0]["issue_grouping_method"],
            "same_spreadsheet_row_with_history",
        )
        self.assertEqual(comments[0]["event_date"], "2026-01-02")
        self.assertEqual(comments[0]["event_date_source"], "reviewer_column")
        self.assertEqual(
            comments[0]["issue_thread_events"][0]["event_type"],
            "discussion_note",
        )
        self.assertEqual(
            summary["extraction_method"],
            "local_structured_spreadsheet",
        )
        viewer_location = SourceLocation.from_record(
            comments[0], "DOC-test", "xlsx",
        )
        self.assertEqual(viewer_location.sheet_name, "Review Comments")
        self.assertEqual(viewer_location.cell_range, "C2")
        self.assertEqual(review, [])

    def test_pipeline_routes_known_workbook_without_rendering_or_extraction(self):
        raw = projectdox_raw()
        source = self.root / "known.xlsx"
        source.write_bytes(b"source")

        class Client:
            model = "test-flash"
            last_usage_metadata = {}
            last_request_metadata = {}

            def verify_spreadsheet_units(self, packet, context):
                self.last_usage_metadata = {
                    "promptTokenCount": 321,
                    "candidatesTokenCount": 12,
                }
                self.last_request_metadata = {
                    "request_bytes": 4321,
                    "attempts": 1,
                    "model": self.model,
                }
                return {
                    "document_verified": True,
                    "template_verified": True,
                    "every_candidate_assigned": True,
                    "same_row_links_correct": True,
                    "verified_group_ids": [
                        group["group_id"] for group in packet["groups"]
                    ],
                    "rejected_group_ids": [],
                    "missing_unit_ids": [],
                    "incorrect_groupings": [],
                    "incorrect_links": [],
                    "verification_summary": "verified",
                }

            def extract_document(self, bundle, context):
                raise AssertionError("known XLSX must not use visual extraction")

            def verify_document(self, bundle, extraction):
                raise AssertionError("known XLSX must not use visual verification")

        pipeline = VisualIngestionPipeline(
            Client(), self.root / "artifacts",
        )
        pipeline.builder._raw_text = (
            lambda source, digest, directory: raw
        )
        comments, responses, _links, summary, review = pipeline.process(
            source,
            "comments&response/known.xlsx",
            {
                "property_hint": "100 Main St",
                "city_hint": "Menlo Park",
                "review_round_hint": "2",
            },
            force=True,
        )
        self.assertEqual((len(comments), len(responses)), (2, 1))
        self.assertEqual(review, [])
        self.assertEqual(
            summary["performance"]["gemini_input_tokens"], 321,
        )
        self.assertEqual(
            summary["performance"]["gemini_spreadsheet_verification_calls"],
            1,
        )
        self.assertEqual(
            summary["performance"]["gemini_extraction_calls"], 0,
        )

    def test_visual_fallback_does_not_repeat_all_workbook_cells(self):
        raw = projectdox_raw()
        scoped = raw_text_for_page_batch(
            raw, [PageImage(2, self.root / "page-0002.jpg")],
        )
        self.assertEqual(scoped["kind"], "xlsx_visual_fallback_metadata")
        self.assertNotIn("rows", scoped["sheets"][0])

    def test_csv_comment_response_columns_use_the_same_unit_model(self):
        raw = {
            "kind": "csv_cells",
            "rows": [
                {
                    "row_number": 1,
                    "values": ["Comment #", "Government Comment", "Response"],
                },
                {
                    "row_number": 2,
                    "values": ["4", "Exact CSV comment.", "Exact CSV response."],
                },
            ],
        }
        schemas = detect_spreadsheet_schemas(raw)
        self.assertEqual(
            schemas[0]["template_id"],
            "generic_comment_response_table_v1",
        )
        evidence = build_spreadsheet_evidence(raw, schemas, {})
        record = evidence["extraction"]["records"][0]
        self.assertEqual(record["exact_comment_text"], "Exact CSV comment.")
        self.assertEqual(record["exact_response_text"], "Exact CSV response.")
        self.assertEqual(record["comment_location"]["sheet_name"], "CSV")
        self.assertEqual(record["comment_location"]["cell_range"], "B2")


if __name__ == "__main__":
    unittest.main()
