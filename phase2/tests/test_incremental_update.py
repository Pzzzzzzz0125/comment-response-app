import unittest
import json
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from phase2 import incremental_update as incremental


def make_xlsx(path: Path, rows: list[list[str]]) -> None:
    sheet_rows = []
    for row_number, values in enumerate(rows, start=1):
        cells = [
            (
                f'<c r="{chr(64 + column_number)}{row_number}" t="inlineStr">'
                f"<is><t>{escape(value)}</t></is></c>"
            )
            for column_number, value in enumerate(values, start=1)
        ]
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Review Comments" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>',
        )


class IncrementalParserTests(unittest.TestCase):
    def test_file_workers_allow_fast_source_to_finish_while_slow_source_waits(self):
        records = [
            {
                "path": "comments&response/site/slow.pdf", "sha256": "slow",
                "document_type": "city_comments",
            },
            {
                "path": "comments&response/site/fast.pdf", "sha256": "fast",
                "document_type": "city_comments",
            },
        ]
        release_slow = threading.Event()
        completion_order = []

        class Pipeline:
            def fork(self):
                return Pipeline()

            def process(self, _source, relative, _context):
                if relative.endswith("slow.pdf"):
                    release_slow.wait(1)
                else:
                    completion_order.append("fast")
                    release_slow.set()
                if relative.endswith("slow.pdf"):
                    completion_order.append("slow")
                return [], [], [], {
                    "source_document": relative,
                    "processing_status": "completed",
                }, []

        with patch.object(
            incremental, "canonicalize_records_before_gemini",
            return_value=(records, []),
        ), patch.object(
            incremental, "prescan_source_group",
            return_value={"files": []},
        ):
            _c, _r, _l, summaries, _q = incremental.process_new_group(
                Path("."), {"likely_city": "Test"}, records, Pipeline(),
                file_workers=2,
            )
        self.assertEqual(completion_order, ["fast", "slow"])
        self.assertEqual(
            [row["source_document"] for row in summaries],
            [records[0]["path"], records[1]["path"]],
        )

    def test_structured_refresh_preserves_manual_confirmed_rematches(self):
        legacy_path = "comments&response/new/site-comments.xlsx"
        confirmed_path = "comments&response/verified/response.xlsx"
        dataset = {
            "comments": [
                {
                    "comment_id": "C-legacy",
                    "source_document": legacy_path,
                    "ingestion_pipeline_version":
                        "adaptive-document-ingestion-v3",
                },
                {
                    "comment_id": "C-confirmed",
                    "source_document": confirmed_path,
                    "ingestion_pipeline_version": "",
                },
                {
                    "comment_id": "C-retry",
                    "source_document": "comments&response/retry/comments.xlsx",
                    "ingestion_pipeline_version":
                        "adaptive-document-ingestion-v4",
                    "search_eligible": False,
                },
                {
                    "comment_id": "C-current",
                    "source_document": "comments&response/current/comments.xlsx",
                    "ingestion_pipeline_version":
                        "adaptive-document-ingestion-v4",
                    "search_eligible": True,
                },
            ],
            "comment_response_links": [
                {
                    "comment_id": "C-legacy",
                    "provenance": "gemini_visual_two_pass",
                },
                {
                    "comment_id": "C-confirmed",
                    "provenance": "all_projects_verified_rematch",
                },
                {
                    "comment_id": "C-retry",
                    "provenance": "local_structured_gemini_verified",
                    "review_status": "needs_review",
                },
                {
                    "comment_id": "C-current",
                    "provenance": "local_structured_gemini_verified",
                    "review_status": "confirmed",
                },
            ],
        }
        self.assertEqual(
            incremental.legacy_structured_refresh_paths(dataset),
            {
                legacy_path,
                "comments&response/retry/comments.xlsx",
            },
        )
        self.assertEqual(
            incremental.legacy_structured_refresh_paths(
                dataset, ["verified"],
            ),
            set(),
        )

    def test_retryable_ingestion_paths_respects_site_scope(self):
        report = {"files": [
            {
                "relative_path": "new/site-a/comments.pdf",
                "processing_status": "failed",
            },
            {
                "relative_path": "new/site-a/responses.pdf",
                "processing_status": "paused_quota",
            },
            {
                "relative_path": "new/site-b/comments.pdf",
                "processing_status": "circuit_open",
            },
            {
                "relative_path": "new/site-a/complete.pdf",
                "processing_status": "completed",
            },
            {
                "relative_path": "new/site-a/ambiguous.pdf",
                "processing_status": "failed",
                "review_reason": (
                    "Gemini request status is unknown after submission; "
                    "automatic resubmission was blocked"
                ),
            },
        ]}
        self.assertEqual(
            incremental.retryable_ingestion_paths(report, ["site-a"]),
            {
                "new/site-a/comments.pdf",
                "new/site-a/responses.pdf",
            },
        )
        self.assertEqual(
            len(incremental.retryable_ingestion_paths(report)), 3,
        )
        self.assertEqual(
            incremental.ambiguous_submission_paths(report, ["site-a"]),
            {"new/site-a/ambiguous.pdf"},
        )

    def test_retryable_ingestion_paths_uses_pre_inventory_failure_state(self):
        previous = {"files": [{
            "relative_path": "new/site/failed.pdf",
            "processing_status": "failed",
        }]}
        rebuilt = {"files": [{
            "relative_path": "new/site/failed.pdf",
            "processing_status": "pending",
        }]}
        self.assertEqual(
            incremental.retryable_ingestion_paths(previous, ["new/"]),
            {"new/site/failed.pdf"},
        )
        self.assertEqual(
            incremental.retryable_ingestion_paths(rebuilt, ["new/"]),
            set(),
        )

    def test_orphaned_pending_processed_path_is_reopened(self):
        report = {"files": [
            {"relative_path": "new/site/orphan.pdf", "processing_status": "pending"},
            {"relative_path": "new/site/represented.pdf", "processing_status": "pending"},
            {"relative_path": "new/other/orphan.pdf", "processing_status": "pending"},
        ]}
        self.assertEqual(
            incremental.orphaned_pending_paths(
                report,
                {
                    "new/site/orphan.pdf", "new/site/represented.pdf",
                    "new/other/orphan.pdf",
                },
                {"new/site/represented.pdf"},
                ["new/site/"],
            ),
            {"new/site/orphan.pdf"},
        )

    def test_group_upsert_replaces_unconfirmed_stable_id(self):
        old_comment = {"comment_id": "C-1", "original_text": "old"}
        old_response = {"response_id": "R-1", "comment_id": "C-1"}
        old_link = {
            "comment_id": "C-1", "response_id": "R-1",
            "match_status": "matched", "review_status": "needs_review",
        }
        new_comment = {"comment_id": "C-1", "original_text": "new"}
        new_response = {"response_id": "R-2", "comment_id": "C-1"}
        new_link = {
            "comment_id": "C-1", "response_id": "R-2",
            "match_status": "matched", "review_status": "needs_review",
        }
        values = incremental.upsert_ingested_group(
            [old_comment], [old_response], [old_link],
            [new_comment], [new_response], [new_link],
        )
        comments, responses, links, incoming_comments, incoming_responses, incoming_links = values
        self.assertEqual(comments, [])
        self.assertEqual(responses, [])
        self.assertEqual(links, [])
        self.assertEqual(incoming_comments, [new_comment])
        self.assertEqual(incoming_responses, [new_response])
        self.assertEqual(incoming_links, [new_link])

    def test_group_upsert_rejects_conflicting_confirmed_text(self):
        with self.assertRaisesRegex(ValueError, "conflicts with confirmed"):
            incremental.upsert_ingested_group(
                [{"comment_id": "C-1", "original_text": "verified"}],
                [],
                [{
                    "comment_id": "C-1", "response_id": "",
                    "match_status": "unmatched", "review_status": "confirmed",
                }],
                [{"comment_id": "C-1", "original_text": "different"}],
                [],
                [{
                    "comment_id": "C-1", "response_id": "",
                    "match_status": "unmatched", "review_status": "needs_review",
                }],
            )

    def test_local_prescan_keeps_archives_as_context(self):
        archive = incremental.fallback_prescan_decision({
            "path": "comments&response/site/package/ARCHIVE/Response Letter.pdf",
            "document_type": "company_response",
            "likely_contains_company_responses": True,
        })
        current = incremental.fallback_prescan_decision({
            "path": "comments&response/site/package/Response Letter.pdf",
            "document_type": "company_response",
            "likely_contains_company_responses": True,
        })
        review = incremental.fallback_prescan_decision({
            "path": "comments&response/site/PLNG-Review.docx",
            "document_type": "review_letter",
            "likely_contains_city_comments": False,
        })
        self.assertEqual(archive["decision"], "context_only")
        self.assertEqual(current["decision"], "full_read")
        self.assertEqual(review["decision"], "full_read")

    def test_prescan_uses_lightweight_client_when_configured(self):
        class Client:
            def __init__(self, name):
                self.name = name
                self.calls = 0

            def pre_scan_sources(self, files, context):
                self.calls += 1
                return {"files": [{
                    "relative_path": files[0]["relative_path"],
                    "decision": "context_only",
                    "document_role": "supporting_source",
                    "reason": self.name,
                    "confidence": 0.9,
                    "linked_topics": [],
                }]}

        strong = Client("strong")
        lite = Client("lite")

        class Pipeline:
            client = strong
            prescan_client = lite

        record = {
            "path": "comments&response/site/plan.pdf",
            "filename": "plan.pdf",
            "extension": ".pdf",
            "document_type": "drawing_or_plan",
            "likely_contains_city_comments": False,
            "likely_contains_company_responses": False,
            "page_count": 1,
        }
        result = incremental.prescan_source_group(
            Path("."), {}, [record], Pipeline(), use_gemini=True,
        )
        self.assertEqual(strong.calls, 0)
        self.assertEqual(lite.calls, 1)
        self.assertEqual(result["files"][0]["reason"], "lite")

    def test_offline_spreadsheet_rows_are_located_but_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.xlsx"
            make_xlsx(path, [
                ["ID", "Reviewer", "TYPE", "VIEW", "ENTER YOUR COMMENT RESPONSE HERE", "DISCUSSION", "CYCLE", "STATUS"],
                ["1", "Building : Reviewer", "Comment Revise Sheet A1.", "", "Updated Sheet A1.", "", "2.0", "Open"],
            ])
            record = {
                "path": "comments&response/site/review.xlsx",
                "sha256": "abc123",
                "likely_city": "San Jose",
                "likely_property_project": "Test Project",
                "likely_review_round": "unknown",
                "primary_sheet": "Review Comments",
                "likely_comment_columns": {
                    "Review Comments": [{"column": "C", "header": "TYPE"}],
                },
                "likely_response_columns": {
                    "Review Comments": [{
                        "column": "E",
                        "header": "ENTER YOUR COMMENT RESPONSE HERE",
                    }],
                },
                "detected_spreadsheet_headers": {
                    "Review Comments": {
                        "row": 1,
                        "columns": [
                            {"column": "A", "header": "ID"},
                            {"column": "B", "header": "Reviewer"},
                            {"column": "G", "header": "CYCLE"},
                            {"column": "H", "header": "STATUS"},
                        ],
                    },
                },
            }
            comments, responses, links, summary, _review = (
                incremental.prepare_offline_spreadsheet_rows(path, record)
            )
            self.assertEqual(comments[0]["review_round"], "2")
            self.assertEqual(comments[0]["source_locator_json"]["cell_range"], "C2")
            self.assertEqual(responses[0]["source_locator_json"]["cell_range"], "E2")
            self.assertEqual(comments[0]["text_trust_status"], "quarantined")
            self.assertFalse(comments[0]["search_eligible"])
            self.assertEqual(links[0]["review_status"], "needs_review")
            self.assertEqual(links[0]["match_status"], "needs_review")
            self.assertEqual(links[0]["match_confidence"], 0.0)
            self.assertEqual(
                summary["verification_result"],
                "deterministic_structure_only_needs_review",
            )

    def test_quick_city_and_prescan_use_bounded_xlsx_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.xlsx"
            make_xlsx(path, [
                ["Reviewer", "Comment", "Response"],
                ["erica.bravo@sanjoseca.gov", "Revise plans.", "Updated."],
            ])
            relative = (
                "comments&response/25-033-701_S_Clover_Ave/review.xlsx"
            )
            city, confidence, evidence, method = incremental.quick_city_for_source(
                path, relative,
            )
            self.assertEqual(city, "San Jose")
            self.assertEqual(confidence, 0.99)
            self.assertEqual(method, "bounded_source_content")
            self.assertTrue(any("sanjoseca.gov" in item for item in evidence))
            snippet = incremental.prescan_text_snippet(path)
            self.assertIn("erica.bravo@sanjoseca.gov", snippet)
            self.assertIn("Revise plans.", snippet)

    def test_city_propagates_to_sibling_files_in_same_site(self):
        files = [
            {
                "relative_path": "comments&response/site/review.xlsx",
                "filename": "review.xlsx", "file_type": "xlsx",
                "file_size_bytes": 10, "city": "San Jose",
                "city_confidence": 0.99,
                "city_evidence": ["source contains sanjoseca.gov"],
            },
            {
                "relative_path": "comments&response/site/plan.pdf",
                "filename": "plan.pdf", "file_type": "pdf",
                "file_size_bytes": 100, "city": "Unknown",
                "city_confidence": 0.0, "city_evidence": [],
            },
        ]
        incremental.resolve_inventory_cities(Path("."), files, {})
        self.assertEqual(files[1]["city"], "San Jose")
        self.assertEqual(
            files[1]["city_resolution_method"], "site_folder_propagation",
        )

    def test_explicit_site_folder_city_overrides_consultant_address(self):
        relative = (
            "new/25-004-18255 Clemson Ave, Saratoga, CA 95070/"
            "Title 24 report.pdf"
        )
        city, confidence, evidence, method = incremental.quick_city_for_source(
            Path(relative), relative, {
                "likely_city": "Redwood City", "city_confidence": 0.99,
                "city_evidence": ["consultant postal address names Redwood City"],
            },
        )
        self.assertEqual(city, "Saratoga")
        self.assertEqual(confidence, 1.0)
        self.assertEqual(method, "explicit_site_folder_address")
        self.assertIn("project folder", evidence[0])

    def test_discovered_city_overrides_stale_unknown_audit_group(self):
        relative = "comments&response/site/review.xlsx"
        inventory = {relative: {
            "path": relative, "filename": "review.xlsx", "extension": ".xlsx",
            "likely_city": "unknown", "likely_property_project": "701 S Clover Ave",
            "likely_review_round": "1",
        }}
        groups = incremental.all_source_groups(inventory, [{
            "relative_path": relative, "filename": "review.xlsx",
            "file_type": "xlsx", "city": "San Jose",
            "city_confidence": 0.99,
            "city_evidence": ["source contains sanjoseca.gov"],
            "city_resolution_method": "bounded_source_content",
        }])
        self.assertEqual(groups[0][0]["likely_city"], "San Jose")
        self.assertEqual(groups[0][1][0]["likely_city"], "San Jose")

    def test_prescan_coalesces_rounds_within_one_site(self):
        groups = [
            (
                {"likely_city": "Cupertino", "likely_review_round": "1"},
                [{
                    "path": "comments&response/site/round-1/comments.docx",
                    "likely_city": "Cupertino", "likely_review_round": "1",
                }],
            ),
            (
                {"likely_city": "Cupertino", "likely_review_round": "2"},
                [{
                    "path": "comments&response/site/round-2/response.pdf",
                    "likely_city": "Cupertino", "likely_review_round": "2",
                }],
            ),
        ]
        coalesced = incremental.coalesce_prescan_groups(groups)
        self.assertEqual(len(coalesced), 1)
        self.assertEqual(len(coalesced[0][1]), 2)
        self.assertEqual(coalesced[0][0]["likely_city"], "Cupertino")
        self.assertEqual(coalesced[0][0]["likely_review_round"], "")

    def test_menlo_matrix_layout_extracts_comment_and_response(self):
        page = "\n".join([
            " Comment Page Ref  Reviewer : Department  Review Comments                                     Applicant Response",
            " ID",
            "                                                                                               Sheet updated.",
            " 130     A0.00     BPC WC3 1 : Building   Remove the deferred system from the cover.",
            "                                            Continue the city comment.",
        ])
        items = incremental.parse_menlo_matrix_pages([page])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["number"], "130")
        self.assertIn("Remove the deferred system", items[0]["comment"])
        self.assertIn("Sheet updated", items[0]["response"])

    def test_sunnyvale_comment_letter_keeps_information_unmatched(self):
        page = """
        1. Planning
        1.) Submit the landscape checklist.
        4. Architectural
        This project requires payment of school fees.
        Sheet A0.1 - Update dates.
        """
        units = incremental.sunnyvale_comment_units([page])
        self.assertEqual(
            [(unit["discipline"], unit["number"]) for unit in units],
            [("Planning", "1"), ("Architectural", "INFO-1"), ("Architectural", "1")],
        )

    def test_menlo_source_group_expands_indirect_correction_files(self):
        def record(path, filename, kind="city_comments", comments=True):
            return {
                "path": path, "filename": filename, "extension": ".pdf",
                "likely_city": "Menlo Park", "likely_property_project": "2311 Warner Range Ave",
                "likely_review_round": "2", "document_type": kind,
                "likely_contains_city_comments": comments,
            }

        selected = record(
            "comments&response/project/2nd Round of Comments/main.docx",
            "main.docx", comments=True,
        )
        structural = record(
            "comments&response/project/2nd Round of Comments/PC2- Structural Calculation-Reviewed-Corrections-Required.pdf",
            "PC2- Structural Calculation-Reviewed-Corrections-Required.pdf",
        )
        response = record(
            "comments&response/project/3rd Submission Package/PC3- Response Letter.pdf",
            "PC3- Response Letter.pdf", "company_response", False,
        )
        inventory = {item["path"]: item for item in [selected, structural, response]}
        summary = {"likely_city": "Menlo Park"}

        expanded = incremental.expand_menlo_source_group(summary, [selected], inventory)
        self.assertEqual([item["path"] for item in expanded], [
            selected["path"], structural["path"], response["path"],
        ])

    def test_grouped_menlo_response_note_links_same_topic_unmatched_comments(self):
        comment = {
            "comment_id": "C-1", "original_text": "Revise the foundation plan and structural calculations.",
            "discipline": "Building", "source_document": "comments&response/project/comments.pdf",
        }
        response = {
            "response_id": "R-1", "comment_id": "C-source",
            "original_text": "Note: Please see updated foundation plan included in the plan set and new Geotechnical report and foundation review letter uploaded.",
            "source_document": "comments&response/project/response.pdf",
            "source_location": "page 2", "source_locator_json": {"pages": [2]},
        }
        link = {
            "link_id": "L-old", "comment_id": "C-1", "response_id": "",
            "match_status": "unmatched", "matching_method": "no_response_in_selected_source",
        }

        review = incremental.apply_grouped_response_notes(
            {"likely_city": "Menlo Park"}, [], [comment], [response], [link],
        )
        self.assertEqual(comment["response_id"], "R-1")
        self.assertEqual(link["response_id"], "R-1")
        self.assertEqual(link["matching_method"], "gemini_grouped_broad_response_note")
        self.assertEqual(link["review_status"], "needs_review")
        self.assertIn("C-1", response["comment_ids"])
        self.assertEqual(len(review), 1)

    def test_normal_single_row_sheet_response_is_not_grouped(self):
        comment = {
            "comment_id": "C-1", "original_text": "Revise the foundation plan.",
            "discipline": "Building", "source_document": "comments&response/project/comments.pdf",
        }
        response = {
            "response_id": "R-1", "comment_id": "C-source",
            "original_text": "Noted. Please see updated sheet A2.00 with new note on the sump pit.",
            "source_document": "comments&response/project/response.pdf",
            "source_location": "page 2", "source_locator_json": {"pages": [2]},
        }
        link = {
            "link_id": "L-old", "comment_id": "C-1", "response_id": "",
            "match_status": "unmatched", "matching_method": "no_response_in_selected_source",
        }

        review = incremental.apply_grouped_response_notes(
            {"likely_city": "Menlo Park"}, [], [comment], [response], [link],
        )
        self.assertEqual(comment.get("response_id", ""), "")
        self.assertEqual(link["response_id"], "")
        self.assertEqual(review, [])

    def test_prescan_priority_does_not_skip_content_screening(self):
        def record(path, filename, kind, comments=False, responses=False):
            return {
                "path": path, "filename": filename, "extension": ".pdf",
                "likely_city": "Menlo Park", "likely_property_project": "2311 Warner Range Ave",
                "likely_review_round": "2", "document_type": kind,
                "likely_contains_city_comments": comments,
                "likely_contains_company_responses": responses,
                "page_count": "20",
            }

        correction = record(
            "comments&response/project/PC2- Structural Calculation-Reviewed-Corrections-Required.pdf",
            "PC2- Structural Calculation-Reviewed-Corrections-Required.pdf",
            "city_comments",
            comments=True,
        )
        support = record(
            "comments&response/project/PC3- Structure calculation.pdf",
            "PC3- Structure calculation.pdf",
            "supporting_document",
        )

        class Client:
            def pre_scan_sources(self, files, context):
                return {"files": [
                    {"relative_path": correction["path"], "decision": "context_only", "document_role": "supporting_source", "reason": "mistaken", "confidence": 0.8, "linked_topics": []},
                    {"relative_path": support["path"], "decision": "context_only", "document_role": "supporting_source", "reason": "support only", "confidence": 0.9, "linked_topics": ["structural"]},
                ]}

        class Pipeline:
            def __init__(self):
                self.client = Client()
                self.processed = []

            def process(self, source, relative, context):
                self.processed.append(relative)
                summary = {
                    "city": "Menlo Park", "property_project": "2311 Warner Range Ave",
                    "review_round": "2", "source_document": relative,
                    "source_type": "test", "comment_count": 0, "response_count": 0,
                    "matched_count": 0, "unmatched_count": 0,
                    "extraction_method": "test", "processing_error": "",
                }
                return [], [], [], summary, []

        pipeline = Pipeline()
        _comments, _responses, _links, summaries, _review = incremental.process_new_group(
            Path("."), {"likely_city": "Menlo Park"}, [correction, support], pipeline,
        )
        self.assertEqual(pipeline.processed, [correction["path"]])
        self.assertEqual(len(summaries), 2)
        support_summary = next(
            row for row in summaries
            if row.get("source_document") == support["path"]
        )
        self.assertEqual(support_summary["processing_status"], "classified")
        self.assertFalse(support_summary["opened"])

    def test_inventory_discovers_legacy_and_new_source_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            legacy = workspace / "comments&response" / "legacy" / "review.pdf"
            incoming = workspace / "new" / "incoming" / "review.pdf"
            legacy.parent.mkdir(parents=True)
            incoming.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy")
            incoming.write_bytes(b"incoming")
            report = incremental.inventory_supported_files(
                workspace, {}, workspace / "report.json",
            )
            paths = {row["relative_path"] for row in report["files"]}
            self.assertEqual(paths, {
                "comments&response/legacy/review.pdf",
                "new/incoming/review.pdf",
            })

    def test_new_source_paths_group_by_actual_site_folder(self):
        self.assertEqual(
            incremental._site_folder("new/site-a/subfolder/review.pdf"),
            "site-a",
        )

    def test_supplied_prescan_context_only_does_not_open_source(self):
        record = {
            "path": "new/site/report.pdf", "sha256": "report",
            "document_type": "supporting_document",
        }

        class Pipeline:
            def process(self, *_args, **_kwargs):
                raise AssertionError("context-only source must not be opened")

        with patch.object(
            incremental, "canonicalize_records_before_gemini",
            return_value=([record], []),
        ):
            _c, _r, _l, summaries, _q = incremental.process_new_group(
                Path("."), {"likely_city": "Test"}, [record], Pipeline(),
                prescan_decisions={record["path"]: {
                    "relative_path": record["path"],
                    "decision": "context_only",
                    "document_role": "supporting_document",
                    "reason": "No comments or responses",
                    "confidence": 0.95,
                    "linked_topics": [],
                }},
            )
        self.assertEqual(summaries[0]["processing_status"], "classified")
        self.assertFalse(summaries[0]["opened"])

    def test_empty_source_is_skipped_before_visual_ingestion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = "new/site/empty-comments.pdf"
            source = root / relative
            source.parent.mkdir(parents=True)
            source.write_bytes(b"")
            record = {
                "path": relative,
                "sha256": "e3b0c442",
                "document_type": "combined_comment_response",
            }

            class Pipeline:
                def process(self, *_args, **_kwargs):
                    raise AssertionError("Visual ingestion must not open a 0-byte source")

            with patch.object(
                incremental, "canonicalize_records_before_gemini",
                return_value=([record], []),
            ), patch.object(
                incremental, "prescan_source_group",
                return_value={"files": []},
            ):
                _c, _r, _l, summaries, _q = incremental.process_new_group(
                    root,
                    {"likely_city": "Menlo Park", "likely_review_round": "2"},
                    [record],
                    Pipeline(),
                    prescan_decisions={relative: {
                        "decision": "full_read",
                        "document_role": "combined_comment_response",
                        "reason": "Filename looks relevant",
                        "confidence": 0.9,
                        "linked_topics": [],
                    }},
                )
            self.assertEqual(summaries[0]["processing_status"], "no_relevant_content")
            self.assertEqual(summaries[0]["verification_result"], "not_run_empty_file")
            self.assertFalse(summaries[0]["opened"])

    def test_administrative_prescan_full_read_is_downgraded_without_comment_signal(self):
        for role in (
            "permit_application", "permit_summary",
            "revision_documentation", "revision_summary",
        ):
            decision = incremental.sanitize_prescan_decision({
                "document_type": "unknown",
                "likely_contains_city_comments": False,
                "likely_contains_company_responses": False,
            }, {
                "decision": "full_read", "document_role": role,
                "reason": "Important project metadata", "confidence": 0.9,
                "linked_topics": ["application"],
            })
            self.assertEqual(decision["decision"], "context_only")

    def test_prescan_repair_archives_old_rows_and_replaces_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "phase2_dataset"
            output.mkdir()
            relative = "comments&response/menlo/PC2-comments.pdf"
            source = root / relative
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            old_comment = {
                "comment_id": "C-old", "city": "Menlo Park", "property_project": "2311 Warner",
                "review_round": "2", "comment_number": "1", "original_text": "Old extraction",
                "source_document": relative, "source_sha256": "old", "response_id": "R-old",
                "match_status": "matched", "source_location": "page 1",
            }
            old_response = {
                "response_id": "R-old", "comment_id": "C-old", "original_text": "Old response",
                "source_document": relative, "source_sha256": "old", "source_location": "page 1",
            }
            old_link = {
                "link_id": "L-old", "comment_id": "C-old", "response_id": "R-old",
                "match_status": "matched", "matching_method": "old",
            }
            (output / "dataset.json").write_text(json.dumps({
                "comments": [old_comment], "responses": [old_response],
                "comment_response_links": [old_link], "sources": [],
                "processed_source_paths": [relative], "processed_source_hashes": {relative: "old"},
            }), encoding="utf-8")
            plan = root / "prescan_plan.json"
            plan.write_text(json.dumps({"groups": [{
                "city": "Menlo Park", "property_project": "2311 Warner",
                "review_round": "2", "files": [{"relative_path": relative, "decision": "full_read"}],
            }]}), encoding="utf-8")
            inventory = {relative: {
                "path": relative, "sha256": "new", "likely_city": "Menlo Park",
                "likely_property_project": "2311 Warner", "likely_review_round": "2",
                "document_type": "city_comments",
            }}
            new_comment = {
                "comment_id": "C-new", "city": "Menlo Park", "property_project": "2311 Warner",
                "review_round": "2", "comment_number": "1", "original_text": "Verified extraction",
                "source_document": relative, "source_sha256": "new", "response_id": "R-new",
                "match_status": "matched", "source_location": "page 1",
            }
            new_response = {
                "response_id": "R-new", "comment_id": "C-new", "original_text": "Verified response",
                "source_document": relative, "source_sha256": "new", "source_location": "page 1",
            }
            new_link = {
                "link_id": "L-new", "comment_id": "C-new", "response_id": "R-new",
                "match_status": "matched", "matching_method": "gemini_visual_verified",
            }

            class Pipeline:
                def process(self, source_path, source_relative, context, force=False):
                    return [new_comment], [new_response], [new_link], {
                        "source_document": relative, "city": "Menlo Park", "property_project": "2311 Warner",
                        "review_round": "2", "comment_count": 1, "response_count": 1,
                        "matched_count": 1, "unmatched_count": 0, "source_type": "test",
                        "extraction_method": "test", "processing_error": "",
                    }, []

            with patch.object(incremental.base, "load_audit", return_value=(inventory, [])):
                result = incremental.run_prescan_repair(
                    root, root / "audit", output, plan, Pipeline(), city="Menlo Park",
                )
            saved = json.loads((output / "dataset.json").read_text(encoding="utf-8"))
            self.assertEqual(result["removed_comments"], 1)
            self.assertEqual(result["inserted_comments"], 1)
            self.assertEqual(saved["comments"][0]["comment_id"], "C-new")
            self.assertEqual(saved["repair_history"][0]["removed_comments"][0]["comment_id"], "C-old")

    def test_prescan_repair_never_replaces_confirmed_rows_with_quarantined_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "phase2_dataset"
            output.mkdir()
            relative = "comments&response/menlo/confirmed-response.pdf"
            source = root / relative
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            original = {
                "comments": [{
                    "comment_id": "C-old", "city": "Menlo Park",
                    "property_project": "2311 Warner", "review_round": "2",
                    "comment_number": "1", "original_text": "Confirmed original",
                    "source_document": relative, "source_sha256": "old",
                    "response_id": "R-old", "match_status": "matched",
                    "source_location": "page 1",
                }],
                "responses": [{
                    "response_id": "R-old", "comment_id": "C-old",
                    "original_text": "Confirmed response",
                    "source_document": relative, "source_sha256": "old",
                    "source_location": "page 1",
                }],
                "comment_response_links": [{
                    "link_id": "L-old", "comment_id": "C-old",
                    "response_id": "R-old", "match_status": "matched",
                    "matching_method": "confirmed", "review_status": "confirmed",
                }],
                "sources": [], "processed_source_paths": [relative],
                "processed_source_hashes": {relative: "old"},
            }
            (output / "dataset.json").write_text(json.dumps(original), encoding="utf-8")
            plan = root / "prescan_plan.json"
            plan.write_text(json.dumps({"groups": [{
                "city": "Menlo Park", "property_project": "2311 Warner",
                "review_round": "2",
                "files": [{"relative_path": relative, "decision": "full_read"}],
            }]}), encoding="utf-8")
            inventory = {relative: {
                "path": relative, "sha256": "new", "likely_city": "Menlo Park",
                "likely_property_project": "2311 Warner", "likely_review_round": "2",
                "document_type": "combined_comment_response",
            }}
            quarantined = {
                "comment_id": "C-new", "city": "Menlo Park",
                "property_project": "2311 Warner", "review_round": "2",
                "comment_number": "1", "original_text": "Unverified replacement",
                "source_document": relative, "source_sha256": "new",
                "response_id": "", "match_status": "unmatched",
                "source_location": "page 1", "text_trust_status": "quarantined",
                "search_eligible": False,
            }

            class Pipeline:
                def process(self, source_path, source_relative, context, force=False):
                    return [quarantined], [], [{
                        "link_id": "L-new", "comment_id": "C-new", "response_id": "",
                        "match_status": "unmatched", "matching_method": "needs_review",
                        "review_status": "needs_review",
                    }], {
                        "source_document": relative, "city": "Menlo Park",
                        "property_project": "2311 Warner", "review_round": "2",
                        "comment_count": 1, "response_count": 0,
                        "matched_count": 0, "unmatched_count": 1,
                        "source_type": "test", "extraction_method": "test",
                        "processing_error": "needs review",
                    }, []

            with patch.object(incremental.base, "load_audit", return_value=(inventory, [])):
                with self.assertRaisesRegex(RuntimeError, "confirmed-record preservation gate"):
                    incremental.run_prescan_repair(
                        root, root / "audit", output, plan, Pipeline(), city="Menlo Park",
                    )
            saved = json.loads((output / "dataset.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, original)

    def test_ingestion_report_aggregates_request_tokens_and_straggler_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "ingestion_report.json"
            report = incremental.write_ingestion_report(
                report_path,
                [{"relative_path": "a.pdf", "processing_status": "pending"}],
                [{
                    "source_document": "a.pdf", "processing_status": "complete",
                    "comment_count": 2, "performance": {
                        "gemini_calls": 1,
                        "request_metrics": [{
                            "input_tokens": 100, "cached_input_tokens": 40,
                            "output_tokens": 20, "thought_tokens": 3,
                            "request_bytes": 7, "response_bytes": 8,
                            "retry_count": 1, "image_count": 2,
                            "evidence_unit_count": 2, "expected_record_count": 2,
                            "actual_record_count": 2, "upload_duration": 0.5,
                            "time_to_first_token": 1.0, "generation_duration": 2.0,
                            "queue_duration": 0.25, "finish_reason": "STOP",
                            "model": "test-model",
                        }],
                    },
                }],
            )
            performance = report["performance"]
            self.assertEqual(performance["gemini_input_tokens"], 100)
            self.assertEqual(performance["gemini_cached_input_tokens"], 40)
            self.assertEqual(performance["gemini_output_tokens"], 20)
            self.assertEqual(performance["retry_count"], 1)
            self.assertEqual(performance["image_count"], 2)
            self.assertEqual(performance["response_bytes"], 8)
            self.assertEqual(performance["finish_reasons"], ["STOP"])
            self.assertEqual(performance["models_used"], ["test-model"])

    def test_pipeline_checkpoint_keeps_stage_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            value = incremental.write_pipeline_checkpoint(
                output, "run-1", {"uploaded": "complete", "parsed": "complete"},
            )
            self.assertEqual(value["schema_version"], incremental.CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(value["stages"]["parsed"]["status"], "complete")
            self.assertTrue(value["stages"]["parsed"]["version"])


if __name__ == "__main__":
    unittest.main()
