import json
import tempfile
import threading
import unittest
from pathlib import Path

from phase2.visual_ingestion import (
    EvidenceBundle,
    GeminiCircuitBreaker,
    GeminiCircuitOpenError,
    PageImage,
    VisualGeminiClient,
    VisualIngestionPipeline,
    compact_direct_text_for_gemini,
    compact_pdf_native_text_for_model,
    document_verified,
    EXTRACTION_INSTRUCTION,
    normalize_document_date_metadata,
    multimodal_parts,
    merge_visual_batches,
    match_verified_extraction,
    select_relevant_pages,
    page_batches,
    raw_text_for_page_batch,
    regression_against_oracle,
    results_to_dataset_rows,
    split_embedded_response_lines,
)


def extracted_record(**overrides):
    record = {
        "record_key": "row-7", "comment_id": "7", "comment_number": "7", "page": 2,
        "exact_comment_text": "Keep  double spaces\nand punctuation: (A).",
        "exact_response_text": "Response stays\nverbatim.",
        "comment_location": {"pages": [2], "description": "left comment cell", "bounding_boxes": [
            {"page": 2, "x_min": 50, "y_min": 100, "x_max": 480, "y_max": 300},
        ]},
        "response_location": {"pages": [2], "description": "right response cell", "bounding_boxes": [
            {"page": 2, "x_min": 500, "y_min": 100, "x_max": 950, "y_max": 300},
        ]},
        "same_visible_row": True, "explicit_shared_comment_id": False,
        "pairing_evidence": "Both cells are in visible row 7", "confidence": 0.99,
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
            "locations_and_boxes_correct": True, "same_visible_row_or_shared_id": True,
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

    def test_pair_and_coverage_gates_are_required_for_new_verification(self):
        value = verification(
            verification_contract_version="pair-coverage-v1",
            pair_verification={
                "passed": False, "checked_record_count": 1,
                "failed_record_ids": ["row-7"], "reason": "wrong response row",
            },
            coverage_verification={
                "passed": True, "visible_comment_count": 1,
                "visible_response_count": 1, "missing_record_ids": [], "reason": "complete",
            },
        )
        self.assertFalse(document_verified(value))
        value["pair_verification"]["passed"] = True
        self.assertTrue(document_verified(value))

    def test_dated_status_line_is_a_response_not_comment_text(self):
        record = {
            "record_key": "C1", "comment_number": "1",
            "exact_comment_text": "Show the driveway connection.\n3/16/2026: complete.",
            "normalized_comment_text": "Show the driveway connection. 3/16/2026: complete.",
            "exact_response_text": "",
            "page_start": 1, "page_end": 1,
            "bounding_boxes": [{"page": 1, "x_min": 100, "y_min": 400, "x_max": 900, "y_max": 500}],
        }
        repaired = split_embedded_response_lines(record)
        self.assertEqual(repaired["exact_comment_text"], "Show the driveway connection.")
        self.assertEqual(repaired["exact_response_text"], "3/16/2026: complete.")
        self.assertEqual(repaired["response_date_iso"], "2026-03-16")
        self.assertTrue(repaired["response_location"]["bounding_boxes"])
        self.assertIn("3/16/2026", repaired["raw_extracted_comment_text"])

    def test_status_split_does_not_change_a_date_inside_prose(self):
        record = {
            "exact_comment_text": "The note references 3/16/2026: complete. as an example.",
            "exact_response_text": "",
        }
        self.assertEqual(split_embedded_response_lines(record), record)

    def test_response_letter_with_structural_terms_is_escalated(self):
        screening = select_relevant_pages([
            "1. The ST pages need to identify all beams to match the cals.\n"
            "Response: all beam sizes match the structural calculations.\n"
            "2. The ridge beam appears to be a 4x10.\n"
            "Response: Structural plans and calculations have been updated."
        ], force_response=True)
        self.assertEqual(screening["pages_selected_for_full_analysis"], [1])
        self.assertEqual(screening["processing_status"], "comments_and_responses_found")
        self.assertEqual(
            screening["page_classifications"][0]["page_class"],
            "comment_response_table",
        )

    def test_explicit_full_read_cannot_be_vetoed_by_local_keyword_routing(self):
        screening = select_relevant_pages([
            "STRUCTURAL COMMENTS: Show roof framing. Clarify H25 clips."
        ], force_full_read=True)
        self.assertEqual(screening["pages_selected_for_full_analysis"], [1])
        self.assertEqual(
            screening["page_classifications"][0]["processing_decision"],
            "full_gemini_extraction:prescan_evidence_page",
        )

    def test_long_full_read_package_skips_plain_plan_attachment_pages(self):
        pages = [
            "Review Comments Applicant Response\n1. Revise the wall.\nResponse: updated",
            *["FLOOR PLAN EXISTING WALL DIMENSIONS" for _ in range(8)],
            "Reviewer Response: PC2: the wall comment remains open",
        ]
        screening = select_relevant_pages(pages, force_full_read=True)
        selected = screening["pages_selected_for_full_analysis"]
        self.assertIn(1, selected)
        self.assertIn(10, selected)
        self.assertNotIn(5, selected)
        self.assertEqual(screening["pages_screened"], list(range(1, 11)))

    def test_dense_pdf_page_sends_exact_annotations_instead_of_full_plan_text(self):
        dense = "STRUCTURAL PLAN NOTES " * 4_000
        compact, scope = compact_pdf_native_text_for_model(
            dense,
            {
                "annotation_evidence": [{
                    "type": "FreeText",
                    "title": "Reviewer",
                    "subject": "Correction",
                    "content": "PC2: Please provide the missing shear-transfer detail.",
                }],
            },
            limit=2_000,
        )
        self.assertEqual(scope, "annotation_and_high_signal_native_text")
        self.assertIn(
            "PC2: Please provide the missing shear-transfer detail.", compact,
        )
        self.assertLess(len(compact), len(dense) // 10)

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
        self.assertEqual(request_context["selected_page_text_complete"], self.bundle.raw_text)

    def test_docx_gemini_text_omits_empty_structural_noise(self):
        raw = {
            "kind": "docx_blocks",
            "blocks": [
                {
                    "index": 1, "kind": "paragraph", "text": "",
                    "style": "", "is_heading": False,
                    "comment_ids": [], "comments": [],
                },
                {
                    "index": 2, "kind": "paragraph",
                    "text": "Exact  source text.", "style": "Heading1",
                    "is_heading": True, "comment_ids": [], "comments": [],
                },
            ],
        }
        compact = compact_direct_text_for_gemini(raw)
        self.assertEqual(compact, {
            "kind": "docx_blocks",
            "blocks": [{
                "index": 2, "kind": "paragraph",
                "text": "Exact  source text.", "style": "Heading1",
                "is_heading": True,
            }],
        })
        self.assertEqual(raw["blocks"][1]["text"], "Exact  source text.")

    def test_verification_request_uses_images_without_resending_complete_raw_text(self):
        parts = multimodal_parts(
            self.bundle,
            {"city_hint": "Menlo Park"},
            {"comments": [{
                "record_key": "c-1",
                "exact_comment_text": "Verify me",
                "normalized_comment_text": "verify me",
                "bounding_boxes": [{
                    "page": 1, "x_min": 1, "y_min": 2,
                    "x_max": 3, "y_max": 4,
                }],
            }]},
        )
        request_context = json.loads(parts[0]["text"])
        self.assertNotIn("selected_page_text_complete", request_context)
        self.assertIn("verification_evidence_policy", request_context)
        self.assertEqual(
            request_context["proposed_extraction_to_verify"]["comments"][0][
                "exact_comment_text"
            ],
            "Verify me",
        )
        proposal = request_context["proposed_extraction_to_verify"]
        self.assertNotIn("normalized_comment_text", proposal["comments"][0])
        self.assertEqual(len(proposal["comments"][0]["bounding_boxes"]), 1)
        self.assertEqual(
            len([part for part in parts if "inlineData" in part]), 2,
        )

    def test_page_batches_cover_every_page_with_overlap(self):
        pages = [PageImage(index, self.root / f"page-{index:04d}.jpg") for index in range(1, 7)]
        batches = page_batches(pages, 3, 1)
        self.assertEqual([[page.page_number for page in batch] for batch in batches], [
            [1, 2, 3], [3, 4, 5], [5, 6],
        ])

    def test_forked_page_batches_use_two_workers(self):
        pages = list(self.bundle.pages)
        raw_pages = list(self.bundle.raw_text["pages"])
        for number in (3, 4):
            path = self.root / f"page-{number:04d}.jpg"
            path.write_bytes(f"image-{number}".encode())
            pages.append(PageImage(number, path))
            raw_pages.append({"page": number, "text": f"RAW {number}"})
        bundle = EvidenceBundle(
            "VI-parallel", self.source, "parallel-sha", "pdf",
            {"kind": "pdf_text_pages", "pages": raw_pages},
            pages, self.root,
        )

        class Tracker:
            def __init__(inner):
                inner.lock = threading.Lock()
                inner.barrier = threading.Barrier(2)
                inner.active = 0
                inner.maximum = 0

        class ForkableClient:
            inline_limit_bytes = 18_000_000

            def __init__(inner, tracker):
                inner.tracker = tracker

            def fork(inner):
                return ForkableClient(inner.tracker)

            def extract_document(inner, request_bundle, _context):
                page = request_bundle.pages[0].page_number
                with inner.tracker.lock:
                    inner.tracker.active += 1
                    inner.tracker.maximum = max(
                        inner.tracker.maximum, inner.tracker.active,
                    )
                try:
                    inner.tracker.barrier.wait(timeout=1)
                finally:
                    with inner.tracker.lock:
                        inner.tracker.active -= 1
                return extraction([extracted_record(
                    record_key=f"row-{page}", comment_id=str(page),
                    comment_number=str(page), page=page,
                    exact_comment_text=f"Comment {page}",
                    exact_response_text="",
                    comment_location={
                        "pages": [page], "description": "comment",
                        "bounding_boxes": [],
                    },
                    response_location={
                        "pages": [], "description": "",
                        "bounding_boxes": [],
                    },
                    same_visible_row=False,
                )])

            def verify_document(inner, _bundle, value):
                key = value["records"][0]["record_key"]
                return verification(records=[{
                    "record_key": key, "comment_captured": True,
                    "response_captured": True,
                    "text_complete_and_verbatim": True,
                    "pairing_correct": True,
                    "locations_and_boxes_correct": True,
                    "same_visible_row_or_shared_id": True,
                    "verified": True, "uncertainty_reason": "",
                }])

        tracker = Tracker()
        pipeline = VisualIngestionPipeline(
            ForkableClient(tracker), self.root / "artifacts",
            batch_pages=1, batch_overlap=0, batch_workers=2,
        )
        pipeline.builder.build = lambda _source: bundle
        comments, _responses, _links, _summary, _review = pipeline.process(
            self.source, "permit.pdf",
            {"city_hint": "Menlo Park", "review_round_hint": "3"},
            force=True,
        )
        self.assertEqual(len(comments), 4)
        self.assertEqual(tracker.maximum, 2)
        self.assertTrue(pipeline._metrics["visual_batch_parallelized"])

    def test_page_batch_keeps_complete_text_for_its_rendered_pages(self):
        raw = {"kind": "pdf_text_pages", "pages": [
            {"page": 1, "text": "one"}, {"page": 2, "text": "two"},
            {"page": 3, "text": "three"},
        ]}
        selected = raw_text_for_page_batch(raw, [PageImage(2, self.root / "page-0002.jpg")])
        self.assertEqual(selected["pages"], [{"page": 2, "text": "two"}])
        self.assertEqual(len(raw["pages"]), 3)

    def test_batch_merge_allows_property_and_role_label_variants(self):
        left = extraction(property="2311 WARNER RANGE AVE", document_type="company_response")
        right = extraction(property="2311 Warner Range Ave, Menlo Park", document_type="applicant_response")
        merged, checked = merge_visual_batches([left, right], [verification(), verification()])
        self.assertFalse(merged["document_uncertain"])
        self.assertTrue(checked["document_verified"])

    def test_verified_batch_boundary_does_not_quarantine_whole_document(self):
        left = extraction()
        right = extraction(
            records=[extracted_record(record_key="row-8", comment_id="8", comment_number="8")],
            document_uncertain=True,
            document_uncertainty_reason="More document pages exist outside this visual batch",
        )
        right_check = verification(records=[{
            "record_key": "row-8", "comment_captured": True, "response_captured": True,
            "text_complete_and_verbatim": True, "pairing_correct": True,
            "locations_and_boxes_correct": True, "same_visible_row_or_shared_id": True,
            "verified": True, "uncertainty_reason": "",
        }])
        merged, checked = merge_visual_batches([left, right], [verification(), right_check])
        self.assertFalse(merged["document_uncertain"])
        self.assertTrue(checked["document_verified"])

    def test_large_request_uses_uploaded_page_uris_without_skipping_pages(self):
        client = VisualGeminiClient("test-key", inline_limit_bytes=1)
        uploads = []
        client._upload_file = lambda page: (uploads.append(page.page_number) or (f"gemini://page-{page.page_number}", "image/jpeg"))
        first = client._parts(self.bundle, {})
        second = client._parts(self.bundle, {}, extraction())
        self.assertEqual(uploads, [1, 2])
        self.assertEqual([part["fileData"]["fileUri"] for part in first if "fileData" in part], ["gemini://page-1", "gemini://page-2"])
        self.assertEqual(len([part for part in second if "fileData" in part]), 2)

    def test_credit_circuit_is_shared_by_forked_clients(self):
        client = VisualGeminiClient("test-key")
        fork = client.fork()
        self.assertIs(client.circuit_breaker, fork.circuit_breaker)
        client.circuit_breaker.trip("prepayment credits depleted")
        with self.assertRaises(GeminiCircuitOpenError):
            fork.circuit_breaker.check()
        self.assertTrue(
            VisualGeminiClient._definitive_quota_error(
                "Your prepayment credits are depleted"
            )
        )
        self.assertFalse(
            VisualGeminiClient._definitive_quota_error("too many requests")
        )

    def test_stage_cache_change_archives_previous_output(self):
        client = VisualGeminiClient("test-key")
        pipeline = VisualIngestionPipeline(client, self.root / "artifacts")
        bundle = self.bundle
        metadata = bundle.artifact_dir / "gemini_cache_metadata.json"
        output = bundle.artifact_dir / "gemini_extraction.json"
        output.write_text(json.dumps({"old": True}), encoding="utf-8")
        first = {"stage": "extraction", "version": 1}
        second = {"stage": "extraction", "version": 2}
        pipeline._write_stage_cache_identity(bundle, "extraction", first)
        pipeline._archive_stage_output_if_identity_changes(bundle, "extraction", second)
        pipeline._write_stage_cache_identity(bundle, "extraction", second)
        history = bundle.artifact_dir / "cache_history"
        self.assertTrue(history.is_dir())
        self.assertTrue(list(history.glob("gemini_extraction-*.json")))
        self.assertFalse(pipeline._stage_cache_is_compatible(bundle, "extraction", first))

    def test_page_group_checkpoint_is_written(self):
        class FakeClient:
            def extract_document(self, request_bundle, context):
                return extraction()

            def verify_document(self, request_bundle, value):
                return verification()

        pipeline = VisualIngestionPipeline(
            FakeClient(), self.root / "artifacts", batch_pages=1, batch_overlap=0,
        )
        pipeline.builder.build = lambda _source: self.bundle
        pipeline.process(
            self.source, "permit.pdf",
            {"city_hint": "Menlo Park", "review_round_hint": "3"},
            force=True,
        )
        checkpoints = json.loads(
            (self.root / "page_checkpoints.json").read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoints["checkpoint_version"], "page-group-v1")
        self.assertEqual(checkpoints["groups"]["1"]["group"]["status"], "complete")

    def test_verified_result_preserves_exact_text_and_pairing(self):
        comments, responses, links, _summary, review = results_to_dataset_rows(
            self.bundle, extraction(), verification(), "comments&response/permit.pdf",
        )
        self.assertEqual(comments[0]["original_text"], "Keep  double spaces\nand punctuation: (A).")
        self.assertEqual(comments[0]["verified_text"], comments[0]["original_text"])
        self.assertEqual(responses[0]["original_text"], "Response stays\nverbatim.")
        self.assertEqual(responses[0]["verified_text"], responses[0]["original_text"])
        self.assertEqual(comments[0]["response_id"], responses[0]["response_id"])
        self.assertEqual(links[0]["response_id"], responses[0]["response_id"])
        self.assertEqual(links[0]["review_status"], "confirmed")
        self.assertTrue(comments[0]["search_eligible"])
        self.assertEqual(comments[0]["text_trust_status"], "verified")
        self.assertEqual(review, [])

    def test_document_date_is_preserved_on_rows_and_audit(self):
        value = extraction(document_date={
            "raw": "Report Date: May 4, 2026", "iso": "2026-05-04",
            "source": "report_date", "page": 1,
            "evidence": "Report Date: May 4, 2026", "confidence": 0.99,
        })
        comments, responses, links, summary, _review = results_to_dataset_rows(
            self.bundle, value, verification(), "comments&response/permit.pdf",
        )
        self.assertEqual(comments[0]["source_document_date"], "2026-05-04")
        self.assertEqual(responses[0]["document_date"]["iso"], "2026-05-04")
        self.assertEqual(links[0]["document_date"]["source"], "report_date")
        self.assertEqual(summary["document_date"]["iso"], "2026-05-04")

    def test_document_date_falls_back_without_overwriting_round(self):
        value = extraction(document_date={
            "raw": "", "iso": "", "source": "unknown", "page": 0,
            "evidence": "", "confidence": 0,
        }, review_round="3")
        bundle = EvidenceBundle(
            "VI-date", Path("2025-05-04-comments.pdf"), "date-sha", "pdf",
            {}, self.bundle.pages[:1], self.root, document_page_count=1,
        )
        metadata = normalize_document_date_metadata(value, bundle)
        self.assertEqual(metadata["iso"], "2025-05-04")
        self.assertEqual(value["review_round"], "3")

    def test_extraction_prompt_requires_physical_document_date(self):
        self.assertIn("document_date", EXTRACTION_INSTRUCTION)
        self.assertIn("Never use a review-round label as a date", EXTRACTION_INSTRUCTION)

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
                "locations_and_boxes_correct": True, "same_visible_row_or_shared_id": False,
                "verified": False, "uncertainty_reason": "Response may belong to row 8",
            }],
        )
        _comments, _responses, links, _summary, review = results_to_dataset_rows(
            self.bundle, extraction(), failed, "comments&response/permit.pdf",
        )
        self.assertEqual(links[0]["review_status"], "needs_review")
        self.assertTrue(any(item["item_type"] == "gemini_visual_document" for item in review))

    def test_nearby_response_without_row_or_shared_id_is_quarantined(self):
        value = extraction([extracted_record(same_visible_row=False, explicit_shared_comment_id=False)])
        comments, _responses, links, _summary, review = results_to_dataset_rows(
            self.bundle, value, verification(), "comments&response/permit.pdf",
        )
        self.assertFalse(comments[0]["search_eligible"])
        self.assertEqual(links[0]["review_status"], "needs_review")
        self.assertIn("same-visible-row", review[0]["reason"])

    def test_unnumbered_adjacent_labelled_docx_pairs_are_confirmed(self):
        separate = {
            "property": "1263 Flickinger Ave", "city": "San Jose",
            "review_round": "", "document_class": "combined",
            "comment_section_complete": True, "review_reason": "",
            "comments": [{
                "record_key": "comment_1", "comment_number": "",
                "department": "", "reviewer": "",
                "exact_comment_text": "Identify all beams.",
                "normalized_comment_text": "Identify all beams.",
                "page_start": 1, "page_end": 1,
                "bounding_boxes": [{
                    "page": 1, "x_min": 100, "y_min": 100,
                    "x_max": 800, "y_max": 150,
                }],
                "confidence": 1.0, "uncertain": False,
                "uncertainty_reason": "",
            }],
            "responses": [{
                "record_key": "response_1", "response_number": "",
                "exact_response_text": "Response: plans updated",
                "page_start": 1, "page_end": 1,
                "bounding_boxes": [{
                    "page": 1, "x_min": 100, "y_min": 160,
                    "x_max": 800, "y_max": 200,
                }],
                "confidence": 1.0, "uncertain": False,
                "uncertainty_reason": "",
            }],
            "additional_markups_referenced": False,
        }
        checked = {
            "document_verified": True, "every_comment_captured": True,
            "every_response_captured": True, "number_sequence_correct": True,
            "continuations_joined_correctly": True, "headers_excluded": True,
            "neighboring_items_separate": True, "no_response_leakage": True,
            "later_markup_check_complete": True, "verification_summary": "ok",
            "comments": [{
                "record_key": "comment_1", "comment_captured": True,
                "text_complete_and_verbatim": True,
                "locations_and_boxes_correct": True, "verified": True,
                "uncertainty_reason": "",
            }],
            "responses": [{
                "record_key": "response_1", "response_captured": True,
                "text_complete_and_verbatim": True,
                "locations_and_boxes_correct": True, "verified": True,
                "uncertainty_reason": "",
            }],
        }
        paired, paired_check = match_verified_extraction(separate, checked)
        self.assertTrue(paired["records"][0]["explicit_structural_pair"])
        bundle = EvidenceBundle(
            "VI-docx", self.source, "docx-sha", "docx",
            {}, self.bundle.pages[:1], self.root, document_page_count=1,
        )
        comments, responses, links, _summary, review = results_to_dataset_rows(
            bundle, paired, paired_check, "new/response.docx",
        )
        self.assertEqual(comments[0]["comment_number"], "item-1")
        self.assertEqual(responses[0]["original_text"], "Response: plans updated")
        self.assertEqual(links[0]["review_status"], "confirmed")
        self.assertEqual(review, [])

    def test_one_rejected_record_does_not_quarantine_verified_siblings(self):
        rows = [
            extracted_record(record_key="row-1", comment_id="1", comment_number="1"),
            extracted_record(record_key="row-2", comment_id="2", comment_number="2"),
        ]
        checked = verification(
            document_verified=False,
            document_structure_verified=True,
            verification_summary="row 2 differs",
            records=[
                {
                    "record_key": "row-1", "comment_captured": True,
                    "response_captured": True, "text_complete_and_verbatim": True,
                    "pairing_correct": True, "locations_and_boxes_correct": True,
                    "same_visible_row_or_shared_id": True, "verified": True,
                    "uncertainty_reason": "",
                },
                {
                    "record_key": "row-2", "comment_captured": True,
                    "response_captured": True, "text_complete_and_verbatim": False,
                    "pairing_correct": True, "locations_and_boxes_correct": True,
                    "same_visible_row_or_shared_id": True, "verified": False,
                    "uncertainty_reason": "text differs",
                },
            ],
        )
        comments, _responses, links, _summary, _review = results_to_dataset_rows(
            self.bundle, extraction(rows), checked, "comments&response/permit.pdf",
        )
        self.assertTrue(comments[0]["search_eligible"])
        self.assertFalse(comments[1]["search_eligible"])
        self.assertEqual(links[0]["review_status"], "confirmed")
        self.assertEqual(links[1]["review_status"], "needs_review")

    def test_low_confidence_or_invalid_pdf_box_is_quarantined(self):
        low = extraction([extracted_record(confidence=0.94)])
        comments, _responses, links, _summary, _review = results_to_dataset_rows(
            self.bundle, low, verification(), "comments&response/permit.pdf",
        )
        self.assertFalse(comments[0]["search_eligible"])
        self.assertEqual(links[0]["review_status"], "needs_review")
        invalid = extracted_record(comment_location={
            "pages": [2], "description": "bad", "bounding_boxes": [
                {"page": 2, "x_min": 600, "y_min": 100, "x_max": 500, "y_max": 300},
            ],
        })
        comments, _responses, links, _summary, _review = results_to_dataset_rows(
            self.bundle, extraction([invalid]), verification(), "comments&response/permit.pdf",
        )
        self.assertFalse(comments[0]["search_eligible"])
        self.assertEqual(links[0]["review_status"], "needs_review")

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
        changed_comment = extraction([extracted_record(exact_comment_text="Verified replacement text")])
        self.assertTrue(regression_against_oracle(changed_comment, oracle, "permit.pdf")["passed"])
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

    def test_verification_cache_change_does_not_repeat_extraction(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def extract_document(inner, bundle, context):
                inner.calls.append("extract")
                return extraction()

            def verify_document(inner, bundle, value):
                inner.calls.append("verify")
                return verification()

        client = FakeClient()
        pipeline = VisualIngestionPipeline(client, self.root / "unused")
        pipeline.builder.build = lambda _source: self.bundle
        pipeline.process(
            self.source, "permit.pdf",
            {"city_hint": "Menlo Park", "review_round_hint": "3"},
            force=True,
        )
        metadata_path = self.root / "gemini_cache_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["verification"]["verification_prompt_version"] = "obsolete"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        pipeline.process(
            self.source, "permit.pdf",
            {"city_hint": "Menlo Park", "review_round_hint": "3"},
        )
        self.assertEqual(client.calls, ["extract", "verify", "verify"])

    def test_response_letter_round_offset_is_stored_without_quarantining(self):
        class FakeClient:
            def extract_document(self, bundle, context):
                return extraction(review_round="5")

            def verify_document(self, bundle, value):
                return verification()

        pipeline = VisualIngestionPipeline(FakeClient(), self.root / "artifacts")
        pipeline.builder.build = lambda _source: self.bundle
        comments, responses, links, _summary, _review = pipeline.process(
            self.source, "permit.pdf", {
                "city_hint": "Menlo Park", "review_round_hint": "4",
                "audit_document_type_hint": "company_response",
            }, force=True,
        )
        self.assertEqual(comments[0]["review_round"], "4")
        self.assertEqual(comments[0]["reviewed_plan_round"], "4")
        self.assertEqual(comments[0]["response_letter_round"], "5")
        self.assertEqual(links[0]["review_status"], "confirmed")

    def test_regression_failure_quarantines_only_affected_row(self):
        second = extracted_record(record_key="row-8", comment_id="8", comment_number="8", exact_comment_text="Second", exact_response_text="Second response")
        value = extraction([extracted_record(exact_response_text="Wrong"), second])
        checked = verification(records=[
            verification()["records"][0],
            {"record_key": "row-8", "comment_captured": True, "response_captured": True,
             "text_complete_and_verbatim": True, "pairing_correct": True,
             "locations_and_boxes_correct": True, "same_visible_row_or_shared_id": True,
             "verified": True, "uncertainty_reason": ""},
        ])
        comments, _responses, links, _summary, _review = results_to_dataset_rows(
            self.bundle, value, checked, "comments&response/permit.pdf",
            {"applicable": True, "passed": False, "failures": [{"comment_number": "7", "reason": "response text differs"}]},
        )
        numbers = {row["comment_id"]: row["comment_number"] for row in comments}
        statuses = {numbers[row["comment_id"]]: row["review_status"] for row in links}
        self.assertEqual(statuses, {"7": "needs_review", "8": "confirmed"})
        self.assertFalse(next(row for row in comments if row["comment_number"] == "7")["search_eligible"])
        self.assertTrue(next(row for row in comments if row["comment_number"] == "8")["search_eligible"])

    def test_batched_pipeline_extracts_and_verifies_every_page(self):
        class FakeClient:
            def __init__(self):
                self.extracted_pages = []

            def extract_document(inner, bundle, context):
                page = bundle.pages[0].page_number
                inner.extracted_pages.append(page)
                box = {"page": page, "x_min": 10, "y_min": 10, "x_max": 400, "y_max": 200}
                response_box = {"page": page, "x_min": 500, "y_min": 10, "x_max": 900, "y_max": 200}
                return extraction([extracted_record(
                    record_key=f"row-{page}", comment_id=str(page), comment_number=str(page), page=page,
                    exact_comment_text=f"Comment {page}", exact_response_text=f"Response {page}",
                    comment_location={"pages": [page], "description": "comment", "bounding_boxes": [box]},
                    response_location={"pages": [page], "description": "response", "bounding_boxes": [response_box]},
                )])

            def verify_document(inner, bundle, value):
                key = value["records"][0]["record_key"]
                return verification(records=[{
                    "record_key": key, "comment_captured": True, "response_captured": True,
                    "text_complete_and_verbatim": True, "pairing_correct": True,
                    "locations_and_boxes_correct": True, "same_visible_row_or_shared_id": True,
                    "verified": True, "uncertainty_reason": "",
                }])

        client = FakeClient()
        pipeline = VisualIngestionPipeline(client, self.root / "artifacts", batch_pages=1)
        pipeline.builder.build = lambda _source: self.bundle
        comments, _responses, links, _summary, _review = pipeline.process(
            self.source, "permit.pdf", {"city_hint": "Menlo Park", "review_round_hint": "3"}, force=True,
        )
        self.assertEqual(client.extracted_pages, [1, 2])
        self.assertEqual([row["comment_number"] for row in comments], ["1", "2"])
        self.assertTrue(all(row["review_status"] == "confirmed" for row in links))

    def test_large_native_text_batch_is_split_before_request(self):
        pages = []
        for page_number in range(1, 6):
            path = self.root / f"large-page-{page_number:04d}.jpg"
            path.write_bytes(f"image-{page_number}".encode())
            pages.append(PageImage(page_number, path))
        bundle = EvidenceBundle(
            "VI-large-batch",
            self.source,
            "large-batch-sha",
            "pdf",
            {
                "kind": "pdf_text_pages",
                "pages": [
                    {"page": page_number, "text": "x" * 6000}
                    for page_number in range(1, 6)
                ],
            },
            pages,
            self.root,
        )

        class FakeClient:
            def __init__(inner):
                inner.page_groups = []

            def extract_document(inner, request_bundle, context):
                inner.page_groups.append(tuple(
                    page.page_number for page in request_bundle.pages
                ))
                records = []
                for page in request_bundle.pages:
                    box = {
                        "page": page.page_number,
                        "x_min": 10,
                        "y_min": 10,
                        "x_max": 400,
                        "y_max": 200,
                    }
                    records.append(extracted_record(
                        record_key=f"row-{page.page_number}",
                        comment_id=str(page.page_number),
                        comment_number=str(page.page_number),
                        page=page.page_number,
                        exact_comment_text=f"Comment {page.page_number}",
                        exact_response_text="",
                        comment_location={
                            "pages": [page.page_number],
                            "description": "comment",
                            "bounding_boxes": [box],
                        },
                        response_location={
                            "pages": [],
                            "description": "",
                            "bounding_boxes": [],
                        },
                        same_visible_row=False,
                    ))
                return extraction(records)

            def verify_document(inner, request_bundle, value):
                return verification(records=[{
                    "record_key": row["record_key"],
                    "comment_captured": True,
                    "response_captured": True,
                    "text_complete_and_verbatim": True,
                    "pairing_correct": True,
                    "locations_and_boxes_correct": True,
                    "same_visible_row_or_shared_id": True,
                    "verified": True,
                    "uncertainty_reason": "",
                } for row in value["records"]])

        client = FakeClient()
        pipeline = VisualIngestionPipeline(
            client,
            self.root / "artifacts",
            batch_pages=4,
            batch_overlap=1,
        )
        pipeline.builder.build = lambda _source: bundle
        comments, _responses, _links, _summary, _review = pipeline.process(
            self.source,
            "permit.pdf",
            {"city_hint": "Menlo Park", "review_round_hint": "3"},
            force=True,
        )
        self.assertEqual(
            client.page_groups,
            [(1, 2, 3), (3, 4), (4, 5)],
        )
        self.assertEqual(
            [row["comment_number"] for row in comments],
            ["1", "2", "3", "4", "5"],
        )

    def test_timed_out_batch_is_retried_as_smaller_page_groups(self):
        third_page_path = self.root / "page-0003.jpg"
        third_page_path.write_bytes(b"image-three")
        timeout_bundle = EvidenceBundle(
            "VI-timeout",
            self.source,
            "timeout-sha",
            "pdf",
            {
                "kind": "pdf_text_pages",
                "pages": [
                    {"page": 1, "text": "RAW ONE"},
                    {"page": 2, "text": "RAW TWO"},
                    {"page": 3, "text": "RAW THREE"},
                ],
            },
            [
                *self.bundle.pages,
                PageImage(3, third_page_path),
            ],
            self.root,
        )

        class FakeClient:
            def __init__(inner):
                inner.page_groups = []

            def extract_document(inner, request_bundle, context):
                page_numbers = tuple(
                    page.page_number for page in request_bundle.pages
                )
                inner.page_groups.append(page_numbers)
                if len(page_numbers) > 1:
                    raise RuntimeError("The read operation timed out")
                page = page_numbers[0]
                box = {
                    "page": page,
                    "x_min": 10,
                    "y_min": 10,
                    "x_max": 400,
                    "y_max": 200,
                }
                return extraction([extracted_record(
                    record_key=f"row-{page}",
                    comment_id=str(page),
                    comment_number=str(page),
                    page=page,
                    exact_comment_text=f"Comment {page}",
                    exact_response_text="",
                    comment_location={
                        "pages": [page],
                        "description": "comment",
                        "bounding_boxes": [box],
                    },
                    response_location={
                        "pages": [],
                        "description": "",
                        "bounding_boxes": [],
                    },
                    same_visible_row=False,
                )])

            def verify_document(inner, request_bundle, value):
                key = value["records"][0]["record_key"]
                return verification(records=[{
                    "record_key": key,
                    "comment_captured": True,
                    "response_captured": True,
                    "text_complete_and_verbatim": True,
                    "pairing_correct": True,
                    "locations_and_boxes_correct": True,
                    "same_visible_row_or_shared_id": True,
                    "verified": True,
                    "uncertainty_reason": "",
                }])

        client = FakeClient()
        pipeline = VisualIngestionPipeline(
            client,
            self.root / "artifacts",
            batch_pages=2,
            batch_overlap=1,
        )
        pipeline.builder.build = lambda _source: timeout_bundle
        comments, _responses, _links, _summary, _review = pipeline.process(
            self.source,
            "permit.pdf",
            {"city_hint": "Menlo Park", "review_round_hint": "3"},
            force=True,
        )
        self.assertEqual(
            client.page_groups,
            [(1, 2), (1,), (2,), (2, 3), (3,)],
        )
        self.assertEqual(
            [row["comment_number"] for row in comments],
            ["1", "2", "3"],
        )
        self.assertTrue(
            (self.root / "adaptive_split.batch-001.json").is_file(),
        )


if __name__ == "__main__":
    unittest.main()
