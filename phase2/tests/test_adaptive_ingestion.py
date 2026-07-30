import json
import tempfile
import unittest
from pathlib import Path

from phase2.incremental_update import (
    canonicalize_records_before_gemini,
    inventory_supported_files,
    write_ingestion_report,
)
from phase2.visual_ingestion import (
    PIPELINE_VERSION,
    match_verified_extraction,
    page_signal_classification,
    pdf_page_features,
    select_relevant_pages,
)


def structured_verification(comment_keys=(), response_keys=(), **overrides):
    value = {
        "document_verified": True,
        "every_comment_captured": True,
        "every_response_captured": True,
        "number_sequence_correct": True,
        "continuations_joined_correctly": True,
        "headers_excluded": True,
        "neighboring_items_separate": True,
        "no_response_leakage": True,
        "later_markup_check_complete": True,
        "verification_summary": "Complete",
        "comments": [{
            "record_key": key, "comment_captured": True,
            "text_complete_and_verbatim": True,
            "locations_and_boxes_correct": True,
            "verified": True, "uncertainty_reason": "",
        } for key in comment_keys],
        "responses": [{
            "record_key": key, "response_captured": True,
            "text_complete_and_verbatim": True,
            "locations_and_boxes_correct": True,
            "verified": True, "uncertainty_reason": "",
        } for key in response_keys],
    }
    value.update(overrides)
    return value


def comment(key, number, text, start=1, end=1):
    return {
        "record_key": key, "comment_number": number,
        "department": "Building", "reviewer": "Reviewer",
        "exact_comment_text": text, "normalized_comment_text": " ".join(text.split()),
        "page_start": start, "page_end": end,
        "bounding_boxes": [
            {"page": page, "x_min": 20, "y_min": 20, "x_max": 900, "y_max": 300}
            for page in range(start, end + 1)
        ],
        "continues_from_previous_page": False, "continues_to_next_page": False,
        "confidence": 0.99, "uncertain": False, "uncertainty_reason": "",
    }


def response(key, number, text, page=1):
    return {
        "record_key": key, "response_number": number, "exact_response_text": text,
        "page_start": page, "page_end": page,
        "bounding_boxes": [
            {"page": page, "x_min": 20, "y_min": 350, "x_max": 900, "y_max": 600},
        ],
        "confidence": 0.99, "uncertain": False, "uncertainty_reason": "",
    }


def extraction(comments=(), responses=(), **overrides):
    value = {
        "property": "2311 Warner Range Ave", "city": "Menlo Park",
        "review_round": "2", "document_class": "government_comments",
        "comment_section_complete": True,
        "comments": list(comments), "responses": list(responses),
        "additional_markups_referenced": False, "review_reason": "",
    }
    value.update(overrides)
    return value


class AdaptiveIngestionTests(unittest.TestCase):
    def test_01_normally_named_response_letter(self):
        page = page_signal_classification(
            "Applicant Response\n1. Please see updated Sheet A1.\n2. Response: revised.",
            1,
        )
        self.assertIn(page["page_class"], {"response_list", "comment_response_table"})
        for field in (
            "native_text_length", "ocr_required", "annotation_count",
            "form_field_count", "drawing_likelihood", "comment_signal_score",
            "response_signal_score", "page_fingerprint",
            "processing_decision",
        ):
            self.assertIn(field, page)

    def test_02_strangely_named_plan_file_is_detected_from_content(self):
        page = page_signal_classification(
            "CIVIL PLAN REVIEW — CORRECTIONS REQUIRED\n"
            "1. Revise the grading plan.\n2. Provide drainage calculations. CBC 1804.",
            1,
        )
        self.assertEqual(page["page_class"], "comment_list")

    def test_03_only_first_comment_section_of_eighty_page_plan_is_selected(self):
        comments = [
            f"PLAN REVIEW COMMENTS\n{i}. Provide the requested detail. CBC 100{i}."
            for i in range(1, 5)
        ]
        drawings = ["FLOOR PLAN ELEVATION SECTION SCALE 1/4"] * 76
        result = select_relevant_pages(comments + drawings)
        self.assertEqual(result["pages_selected_for_full_analysis"], [1, 2, 3, 4])
        self.assertTrue(result["comment_section_transition_detected"])

    def test_04_later_drawing_markup_is_selected_when_referenced(self):
        pages = [
            "PLAN REVIEW COMMENTS\n1. Revise the plan. See comments marked on plans.",
            "2. Provide calculations. CBC 1604.",
            "FLOOR PLAN ELEVATION SECTION SCALE",
            "FLOOR PLAN ELEVATION SECTION SCALE",
            "FLOOR PLAN ELEVATION SECTION SCALE",
        ] + ["FLOOR PLAN ELEVATION SECTION SCALE"] * 14 + ["MARKUP\nA1. cloud"]
        result = select_relevant_pages(pages)
        self.assertTrue(result["additional_markup_detected"])
        self.assertIn(20, result["pages_selected_for_full_analysis"])

    def test_05_scanned_page_becomes_comment_page_after_ocr_text(self):
        before = page_signal_classification("", 1)
        after = page_signal_classification(
            "CORRECTIONS REQUIRED\n1. Provide structural detail.\n2. Revise foundation plan.",
            1,
        )
        self.assertEqual(before["page_class"], "uncertain")
        self.assertEqual(after["page_class"], "comment_list")

    def test_06_pdf_with_no_comments_is_no_relevant_content(self):
        result = select_relevant_pages([
            "Project cover and contact information. Owner, architect, consultant, address, "
            "telephone, email, parcel information, and general administrative metadata.",
            "Geotechnical supporting report laboratory test results, boring logs, soil "
            "descriptions, moisture measurements, and engineering background information.",
        ])
        self.assertEqual(result["processing_status"], "no_relevant_content")
        self.assertEqual(result["pages_selected_for_full_analysis"], [])

    def test_07_cross_page_comment_is_preserved_as_one_record(self):
        item = comment("c-7", "7", "First-page text\ncontinued exactly on page two.", 1, 2)
        matched, checked = match_verified_extraction(
            extraction([item]), structured_verification(["c-7"]),
        )
        self.assertEqual(len(matched["records"]), 1)
        self.assertEqual(matched["records"][0]["comment_location"]["pages"], [1, 2])
        self.assertTrue(checked["records"][0]["verified"])

    def test_08_neighboring_comments_remain_separate(self):
        matched, _checked = match_verified_extraction(
            extraction([
                comment("c-1", "1", "Provide detail one."),
                comment("c-2", "2", "Provide detail two."),
            ]),
            structured_verification(["c-1", "c-2"]),
        )
        self.assertEqual(
            [row["exact_comment_text"] for row in matched["records"]],
            ["Provide detail one.", "Provide detail two."],
        )

    def test_09_combined_document_is_matched_only_after_verification(self):
        matched, checked = match_verified_extraction(
            extraction(
                [comment("c-1", "1", "Revise Sheet A1.")],
                [response("r-1", "1", "Sheet A1 has been revised.")],
                document_class="combined",
                review_reason="Combined response table.",
            ),
            structured_verification(["c-1"], ["r-1"]),
        )
        self.assertEqual(matched["records"][0]["exact_response_text"], "Sheet A1 has been revised.")
        self.assertTrue(matched["records"][0]["explicit_shared_comment_id"])
        self.assertFalse(matched["document_uncertain"])
        self.assertTrue(checked["records"][0]["pairing_correct"])

    def test_10_reimport_same_hash_reuses_cached_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "comments&response" / "project" / "response.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"same")
            report = root / "report.json"
            first = inventory_supported_files(root, {}, report)
            first["files"][0].update({
                "processing_status": "responses_found", "opened": True,
                "ingestion_pipeline_version": PIPELINE_VERSION,
            })
            write_ingestion_report(report, first["files"], [])
            second = inventory_supported_files(root, {}, report)
            self.assertEqual(second["totals"]["cached_files"], 1)
            self.assertTrue(second["files"][0]["cache_reused_from"])

    def test_11_changed_same_filename_gets_new_pending_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "comments&response" / "project" / "comments.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"version-one")
            report = root / "report.json"
            first = inventory_supported_files(root, {}, report)
            first_hash = first["files"][0]["sha256"]
            source.write_bytes(b"version-two-is-different")
            second = inventory_supported_files(root, {}, report)
            self.assertNotEqual(first_hash, second["files"][0]["sha256"])
            self.assertEqual(second["files"][0]["processing_status"], "pending")

    def test_12_uncertain_file_is_needs_review_not_skipped(self):
        result = select_relevant_pages(["", "", ""])
        self.assertEqual(result["processing_status"], "needs_review")
        self.assertNotEqual(result["processing_status"], "no_relevant_content")

    def test_pdf_annotation_marker_never_silently_skips_without_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "marked.pdf"
            source.write_bytes(b"%PDF-1.4\n/Annots []\n/AcroForm <<>>")
            features = pdf_page_features(source, 3)
            if not features["supported"]:
                self.assertTrue(
                    features["conservative_full_document_escalation"]
                )
                self.assertEqual(
                    features["document_markers"], ["annotations", "acroform"],
                )

    def test_processing_report_totals_reconcile_and_keeps_every_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "comments&response" / "project"
            folder.mkdir(parents=True)
            (folder / "odd civil reviewed.pdf").write_bytes(b"a")
            (folder / "table.csv").write_text("comment,response\none,two\n", encoding="utf-8")
            report = inventory_supported_files(root, {}, root / "report.json")
            self.assertEqual(report["totals"]["discovered_files"], 2)
            self.assertTrue(report["totals"]["inventory_reconciles"])
            self.assertEqual(report["totals"]["pending_files"], 2)
            self.assertEqual(len(report["files"]), 2)

    def test_pre_gemini_canonicalization_collapses_binary_and_text_aliases(self):
        class Builder:
            def content_fingerprint(self, path):
                digest = "a" * 64 if "one" in path.name else "b" * 64
                return digest, "same-normalized-content"

        class Pipeline:
            builder = Builder()

        records = [
            {"path": "site/one.pdf", "sha256": "a" * 64},
            {"path": "site/copy-one.pdf", "sha256": "a" * 64},
            {"path": "site/reexport.pdf", "sha256": "b" * 64},
        ]
        canonical, aliases = canonicalize_records_before_gemini(
            Path("."), records, Pipeline(),
        )
        self.assertEqual(len(canonical), 1)
        self.assertEqual(len(aliases), 2)
        self.assertEqual(
            {row["duplicate_reason"] for row in aliases},
            {"identical_binary_sha256", "identical_normalized_content"},
        )


if __name__ == "__main__":
    unittest.main()
