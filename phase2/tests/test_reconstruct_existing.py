import copy
import unittest

from phase2.evidence_model import build_evidence_model, relationship_snapshot
from phase2.reconstruct_existing import backfill_dataset
from phase2.visual_ingestion import (
    apply_reconstruction_correction,
    reconstruction_correction_required,
)
from web_app.data_trust import verified_text
from web_app.text_reconstruction import (
    attach_reconstruction,
    build_display_structure,
    reconstruct_verbatim_text,
)


class ReconstructionTests(unittest.TestCase):
    def test_local_reconstruction_keeps_words_numbers_and_negation(self):
        value = "The 12\" wall must not be reduced.\n\n_x000d_\nProvide detail A5."
        result = reconstruct_verbatim_text(value)
        self.assertIn('12" wall must not be reduced.', result)
        self.assertIn("Provide detail A5.", result)
        self.assertNotIn("_x000d_", result.casefold())

    def test_display_structure_is_additive_and_structural(self):
        text = "Requirement:\n\na. Show the wall.\nb. Label Sheet A5."
        blocks = build_display_structure(text)
        self.assertEqual(blocks[0]["type"], "heading")
        self.assertTrue(any(block["type"] == "list_item" for block in blocks))

    def test_trusted_reconstruction_is_used_but_untrusted_is_not(self):
        trusted = attach_reconstruction({
            "original_text": "Please provide the\nwall.",
            "verified_text": "Please provide the\nwall.",
            "text_trust_status": "verified",
            "verification_status": "confirmed",
        }, role="comment")
        self.assertEqual(verified_text(trusted), "Please provide the wall.")
        untrusted = attach_reconstruction({
            "original_text": "Please provide the\nwall.",
            "verified_text": "",
            "text_trust_status": "quarantined",
            "verification_status": "needs_review",
        }, role="comment")
        self.assertEqual(verified_text(untrusted), "Please provide the\nwall.")

    def test_existing_backfill_preserves_relationship_snapshot(self):
        dataset = {
            "comments": [{
                "comment_id": "C1", "response_id": "R1",
                "original_text": "Show the\nwall.",
                "verified_text": "Show the\nwall.",
                "text_trust_status": "verified", "verification_status": "confirmed",
                "source_document": "x.pdf", "source_page": 1,
                "source_locator_json": {"page": 1, "bbox": [1, 2, 3, 4]},
                "review_round": "PC1", "event_date_raw": "3/16/2026",
                "issue_thread_id": "T1",
            }],
            "responses": [{
                "response_id": "R1", "comment_id": "C1", "original_text": "Noted.",
                "verified_text": "Noted.", "text_trust_status": "verified",
                "verification_status": "confirmed", "source_document": "x.pdf",
                "source_page": 1, "source_locator_json": {"page": 1, "bbox": [1, 5, 3, 6]},
            }],
            "comment_response_links": [{"link_id": "L1", "comment_id": "C1", "response_id": "R1"}],
            "issue_event_index": {"T1": {"member_comment_ids": ["C1"], "event_ids": ["E1"]}},
        }
        before = relationship_snapshot(dataset)
        updated, report = backfill_dataset(dataset)
        self.assertTrue(report["relationship_graph_unchanged"])
        self.assertEqual(before, relationship_snapshot(updated))
        self.assertEqual(updated["comments"][0]["text_reconstructed"], "Show the wall.")
        self.assertEqual(updated["responses"][0]["text_reconstructed"], "Noted.")
        self.assertTrue(updated["comments"][0]["source_unit_ids"])

    def test_evidence_projection_carries_representation_without_using_it_as_identity(self):
        row = {
            "comment_id": "C1", "canonical_comment_id": "CC1",
            "original_text": "Show the\nwall.", "verified_text": "Show the\nwall.",
            "text_reconstructed": "Show the wall.", "text_raw": "Show the\nwall.",
            "normalized_identity_text_v2": "show the wall.",
            "normalized_search_text_v2": "show the wall.",
            "display_structure": [{"type": "paragraph", "start": 0, "end": 14}],
            "source_unit_ids": ["SU-1"], "reconstruction": {"version": "reconstruction-v1", "verified": True},
            "verification_status": "confirmed", "text_trust_status": "verified",
            "source_document": "x.pdf", "source_page": 1,
            "source_locator_json": {"page": 1, "bbox": [1, 2, 3, 4]},
            "source_sha256": "sha", "city": "Test", "site_id": "S1", "project_id": "P1",
            "review_round": "PC1", "event_date_raw": "3/16/2026",
        }
        dataset = {"comments": [row], "responses": [], "comment_response_links": [], "sources": [], "source_files": {}}
        model = build_evidence_model(dataset)
        event = model["canonical_events"][0]
        self.assertEqual(event["text_representation"]["text_reconstructed"], "Show the wall.")
        self.assertEqual(event["source_occurrence_ids"], [model["source_occurrences"][0]["source_occurrence_id"]])

    def test_correction_is_bounded_and_lexically_safe(self):
        self.assertTrue(reconstruction_correction_required({"correction_required": True}))
        self.assertFalse(reconstruction_correction_required({
            "missing_visible_comments": ["C2"],
            "incorrect_page_locations": ["C1"],
        }))
        source = {
            "records": [{
                "record_key": "C1", "exact_comment_text": "Show the\nwall.",
                "text_reconstructed": "Show the wall.",
            }],
        }
        updated, report = apply_reconstruction_correction(source, {
            "correction_required": True,
            "correction_codes": ["artificial_line_break"],
            "corrections": [{
                "record_key": "C1", "role": "record",
                "corrected_text_reconstructed": "Show the wall.",
                "reason_code": "artificial_line_break",
            }],
        })
        self.assertEqual(updated["records"][0]["exact_comment_text"], "Show the\nwall.")
        self.assertEqual(len(report["accepted"]), 1)
        rejected, rejection_report = apply_reconstruction_correction(source, {
            "correction_required": True,
            "corrections": [{
                "record_key": "C1", "role": "record",
                "corrected_text_reconstructed": "Summarize the wall.",
                "reason_code": "paraphrase",
            }],
        })
        self.assertEqual(rejected, source)
        self.assertEqual(rejection_report["rejected"][0]["reason"], "lexical_safety_failed")


if __name__ == "__main__":
    unittest.main()
