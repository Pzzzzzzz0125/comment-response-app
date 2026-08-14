import json
import tempfile
import unittest
from pathlib import Path

from phase2.evidence_model import (
    STAGES,
    build_evidence_model,
    date_provenance,
    materialize_evidence_model,
    round_provenance,
)


class EvidenceModelTests(unittest.TestCase):
    def _dataset(self):
        text = "Provide the approved wall assembly on sheet A5."
        source_a = "comments&response/Test/PC2-review.pdf"
        source_b = "comments&response/Test/PC2-review-copy.pdf"
        comments = []
        responses = []
        links = []
        for index, source in enumerate((source_a, source_b), start=1):
            comment_id = f"C-{index}"
            response_id = f"R-{index}"
            comments.append({
                "comment_id": comment_id,
                "canonical_comment_id": "CC-wall-assembly",
                "original_text": text,
                "verified_text": text,
                "verification_status": "confirmed",
                "source_document": source,
                "source_page": 2,
                "source_locator_json": {"page": 2, "bbox": [10, 20, 300, 50]},
                "source_sha256": f"sha-{index}",
                "source_row": index,
                "city": "Test City",
                "site_id": "SITE-1",
                "project_id": "PROJECT-1",
                "review_round": "PC2",
                "review_round_source": "document_header",
                "event_date_raw": "3/16/2026",
                "response_id": response_id,
            })
            responses.append({
                "response_id": response_id,
                "comment_id": comment_id,
                "original_text": "See revised sheet A5.",
                "verified_text": "See revised sheet A5.",
                "source_document": source,
                "source_page": 2,
                "source_locator_json": {"page": 2, "bbox": [10, 60, 300, 80]},
                "source_sha256": f"sha-{index}",
            })
            links.append({
                "comment_id": comment_id,
                "response_id": response_id,
                "verification_status": "confirmed",
                "review_status": "confirmed",
                "coverage_verification_status": "confirmed",
            })
        return {
            "source_files": {
                "SF-a": {"folder_path": "comments&response/Test", "filename": "PC2-review.pdf", "binary_sha256": "sha-1"},
                "SF-b": {"folder_path": "comments&response/Test", "filename": "PC2-review-copy.pdf", "binary_sha256": "sha-2"},
            },
            "sources": [
                {"source_document": source_a, "source_file_id": "SF-a", "page_count": 3, "processing_status": "completed"},
                {"source_document": source_b, "source_file_id": "SF-b", "page_count": 3, "processing_status": "completed"},
            ],
            "comments": comments,
            "responses": responses,
            "comment_response_links": links,
            "issue_event_index": {},
            "ingestion_pipeline_version": "test-pipeline",
        }

    def test_explicit_body_date_beats_filename_and_round_is_not_a_date(self):
        result = date_provenance(
            {"event_date_raw": "3/16/2026", "review_round": "PC2"},
            {"filename": "PC2-review-2025.pdf"},
        )
        self.assertEqual(result["value"], "2026-03-16")
        self.assertEqual(result["source"], "document_body")

    def test_reviewed_plan_round_does_not_inherit_later_response_round(self):
        result = round_provenance({
            "reviewed_plan_round": "PC4",
            "review_round": "PC5",
            "review_round_metadata": {
                "value": "PC5", "raw": "PC5", "source": "document_header", "confidence": 0.99,
            },
        })
        self.assertEqual(result["value"], "PC4")
        self.assertEqual(result["source"], "reviewed_plan_round")

    def test_same_canonical_event_keeps_multiple_source_occurrences(self):
        model = build_evidence_model(self._dataset())
        self.assertEqual(model["counts"]["canonical_events"], 1)
        self.assertEqual(model["counts"]["source_occurrences"], 4)
        event = model["canonical_events"][0]
        self.assertEqual(event["confirmation"]["status"], "confirmed")
        self.assertTrue(event["search_eligible"])
        self.assertEqual(sorted(event["comment_ids"]), ["C-1", "C-2"])

    def test_duplicate_extraction_at_same_location_is_one_source_occurrence(self):
        dataset = self._dataset()
        duplicate = dict(dataset["comments"][0])
        duplicate["comment_id"] = "C-duplicate"
        duplicate["response_id"] = ""
        dataset["comments"].append(duplicate)
        model = build_evidence_model(dataset)
        self.assertEqual(model["counts"]["source_occurrences"], 4)
        self.assertEqual(len(model["canonical_events"][0]["source_occurrence_ids"]), 4)

    def test_checkpoint_projection_contains_every_pipeline_stage(self):
        model = build_evidence_model(self._dataset())
        self.assertEqual(model["stages"], list(STAGES))
        self.assertEqual(len(model["checkpoints"]), 2)
        self.assertEqual(set(model["checkpoints"][0]["stages"]), set(STAGES))

    def test_canonical_identity_is_scoped_to_review_round(self):
        dataset = self._dataset()
        later = dict(dataset["comments"][0])
        later["comment_id"] = "C-later"
        later["review_round"] = "PC3"
        later["event_date_raw"] = "4/16/2026"
        later["response_id"] = ""
        dataset["comments"] = [dataset["comments"][0], later]
        dataset["responses"] = [dataset["responses"][0]]
        dataset["comment_response_links"] = [dataset["comment_response_links"][0]]
        model = build_evidence_model(dataset)
        self.assertEqual(model["counts"]["canonical_events"], 2)

    def test_materialize_writes_sidecar_and_dataset_pointer(self):
        dataset = self._dataset()
        with tempfile.TemporaryDirectory() as directory:
            model = materialize_evidence_model(dataset, Path(directory))
            sidecar = Path(directory) / "evidence_model.json"
            self.assertTrue(sidecar.is_file())
            self.assertEqual(json.loads(sidecar.read_text())["counts"], model["counts"])
            self.assertEqual(dataset["evidence_model"]["path"], "evidence_model.json")


if __name__ == "__main__":
    unittest.main()
