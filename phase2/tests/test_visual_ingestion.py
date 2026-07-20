import json
import tempfile
import unittest
from pathlib import Path

from phase2.visual_ingestion import (
    EvidenceBundle,
    PageImage,
    VisualGeminiClient,
    VisualIngestionPipeline,
    multimodal_parts,
    regression_against_oracle,
    results_to_dataset_rows,
)


def extracted_record(**overrides):
    record = {
        "record_key": "row-7", "comment_number": "7",
        "exact_comment_text": "Keep  double spaces\nand punctuation: (A).",
        "exact_response_text": "Response stays\nverbatim.",
        "comment_location": {"pages": [2], "description": "left comment cell", "bounding_boxes": []},
        "response_location": {"pages": [2], "description": "right response cell", "bounding_boxes": []},
        "uncertain": False, "uncertainty_reason": "",
    }
    record.update(overrides)
    return record


def extraction(records=None, **overrides):
    result = {
        "property": "100 Main St", "city": "Menlo Park", "review_round": "3",
        "document_type": "combined comment response form", "document_uncertain": False,
        "document_uncertainty_reason": "", "records": records if records is not None else [extracted_record()],
    }
    result.update(overrides)
    return result


def verification(**overrides):
    result = {
        "document_verified": True, "every_comment_captured": True,
        "every_response_captured": True, "verification_summary": "Complete",
        "records": [{
            "record_key": "row-7", "comment_captured": True, "response_captured": True,
            "text_complete_and_verbatim": True, "pairing_correct": True,
            "verified": True, "uncertainty_reason": "",
        }],
    }
    result.update(overrides)
    return result


class VisualIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "permit.pdf"
        self.source.write_bytes(b"source")
        page1 = self.root / "page-0001.jpg"; page1.write_bytes(b"image-one")
        page2 = self.root / "page-0002.jpg"; page2.write_bytes(b"image-two")
        self.bundle = EvidenceBundle(
            "VI-test", self.source, "abc123", "pdf",
            {"kind": "pdf_text_pages", "pages": [{"page": 1, "text": "RAW ONE"}, {"page": 2, "text": "RAW TWO"}]},
            [PageImage(1, page1), PageImage(2, page2)], self.root,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_multimodal_request_contains_every_page_and_complete_raw_text(self):
        parts = multimodal_parts(self.bundle, {"city_hint": "Menlo Park"})
        images = [part for part in parts if "inlineData" in part]
        labels = [part["text"] for part in parts if part.get("text", "").startswith("ORIGINAL RENDERED PAGE")]
        self.assertEqual(len(images), 2)
        self.assertEqual(labels, [
            "ORIGINAL RENDERED PAGE 1 OF 2 — inspect the entire image.",
            "ORIGINAL RENDERED PAGE 2 OF 2 — inspect the entire image.",
        ])
        request_context = json.loads(parts[0]["text"])
        self.assertEqual(request_context["direct_extracted_text_complete"], self.bundle.raw_text)

    def test_large_request_uses_uploaded_page_uris_without_skipping_pages(self):
        client = VisualGeminiClient("test-key", inline_limit_bytes=1)
        uploads = []
        client._upload_file = lambda page: (uploads.append(page.page_number) or (f"gemini://page-{page.page_number}", "image/jpeg"))
        first = client._parts(self.bundle, {})
        second = client._parts(self.bundle, {}, extraction())
        self.assertEqual(uploads, [1, 2])
        self.assertEqual([part["fileData"]["fileUri"] for part in first if "fileData" in part], ["gemini://page-1", "gemini://page-2"])
        self.assertEqual(len([part for part in second if "fileData" in part]), 2)

    def test_verified_result_preserves_exact_text_and_pairing(self):
        comments, responses, links, _summary, review = results_to_dataset_rows(
            self.bundle, extraction(), verification(), "comments&response/permit.pdf",
        )
        self.assertEqual(comments[0]["original_text"], "Keep  double spaces\nand punctuation: (A).")
        self.assertEqual(responses[0]["original_text"], "Response stays\nverbatim.")
        self.assertEqual(comments[0]["response_id"], responses[0]["response_id"])
        self.assertEqual(links[0]["response_id"], responses[0]["response_id"])
        self.assertEqual(links[0]["review_status"], "confirmed")
        self.assertEqual(review, [])

    def test_uncertain_extraction_is_needs_review_even_if_verifier_says_true(self):
        value = extraction([extracted_record(uncertain=True, uncertainty_reason="row boundary is unclear")])
        comments, responses, links, _summary, review = results_to_dataset_rows(
            self.bundle, value, verification(), "comments&response/permit.pdf",
        )
        self.assertEqual(comments[0]["human_review_status"], "needs_review")
        self.assertEqual(responses[0]["human_review_status"], "needs_review")
        self.assertEqual(links[0]["review_status"], "needs_review")
        self.assertIn("row boundary is unclear", review[0]["reason"])

    def test_pairing_or_document_completeness_failure_is_needs_review(self):
        failed = verification(
            document_verified=False, every_response_captured=False,
            verification_summary="A continuation response is missing",
            records=[{
                "record_key": "row-7", "comment_captured": True, "response_captured": True,
                "text_complete_and_verbatim": True, "pairing_correct": False,
                "verified": False, "uncertainty_reason": "Response may belong to row 8",
            }],
        )
        _comments, _responses, links, _summary, review = results_to_dataset_rows(
            self.bundle, extraction(), failed, "comments&response/permit.pdf",
        )
        self.assertEqual(links[0]["review_status"], "needs_review")
        self.assertTrue(any(item["item_type"] == "gemini_visual_document" for item in review))

    def test_confirmed_reference_regression_checks_count_text_and_response(self):
        oracle = {
            "comments": [{"comment_id": "C-1", "original_text": "immutable older text"}],
            "responses": [{"response_id": "R-1", "original_text": "Response stays verbatim."}],
            "comment_response_links": [{
                "comment_id": "C-1", "response_id": "R-1", "provenance": "document_structure_rematch",
                "source_pdf": "folder/permit.pdf", "city_comment_id": "7",
                "imported_current_round_comment_text": "Keep double spaces and punctuation: (A).",
            }],
        }
        passed = regression_against_oracle(extraction(), oracle, "permit.pdf")
        self.assertTrue(passed["passed"])
        bad = extraction([extracted_record(exact_response_text="Wrong response")])
        failed = regression_against_oracle(bad, oracle, "permit.pdf")
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["failures"][0]["reason"], "response text differs from confirmed reference")

    def test_pipeline_preserves_raw_extraction_verification_and_regression_artifacts(self):
        oracle = {
            "comments": [{"comment_id": "C-1", "original_text": "old"}],
            "responses": [{"response_id": "R-1", "original_text": "Response stays verbatim."}],
            "comment_response_links": [{
                "comment_id": "C-1", "response_id": "R-1", "provenance": "document_structure_rematch",
                "source_pdf": "permit.pdf", "city_comment_id": "7",
                "imported_current_round_comment_text": "Keep double spaces and punctuation: (A).",
            }],
        }
        oracle_path = self.root / "oracle.json"
        oracle_path.write_text(json.dumps(oracle), encoding="utf-8")

        class FakeClient:
            def __init__(self): self.calls = []
            def extract_document(inner, bundle, context): inner.calls.append("extract"); return extraction()
            def verify_document(inner, bundle, value): inner.calls.append("verify"); return verification()

        client = FakeClient()
        pipeline = VisualIngestionPipeline(client, self.root / "unused", oracle_path)
        pipeline.builder.build = lambda _source: self.bundle
        result = pipeline.process(self.source, "permit.pdf", {"city_hint": "Menlo Park", "review_round_hint": "3"}, force=True)
        self.assertEqual(client.calls, ["extract", "verify"])
        self.assertEqual(result[2][0]["review_status"], "confirmed")
        for name in ("gemini_extraction.json", "gemini_verification.json", "confirmed_reference_regression.json", "audit.json"):
            self.assertTrue((self.root / name).is_file(), name)
        pipeline.process(self.source, "permit.pdf", {"city_hint": "Menlo Park", "review_round_hint": "3"})
        self.assertEqual(client.calls, ["extract", "verify"])


if __name__ == "__main__":
    unittest.main()
