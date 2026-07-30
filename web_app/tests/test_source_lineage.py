import unittest
from pathlib import Path

from web_app.source_lineage import mark_copied_source_documents


WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = "comments&response/25-001-2311_warner_range_ave_menlopark/building"


def row(comment_id, source, review_round, text, digest, **extra):
    value = {
        "comment_id": comment_id,
        "city": "Menlo Park",
        "property_project": "25 001 2311 Warner Range Ave",
        "review_round": review_round,
        "source_document": source,
        "source_sha256": digest,
        "original_text": text,
        "discipline": "Building",
        "search_eligible": True,
    }
    value.update(extra)
    return value


class SourceLineageTests(unittest.TestCase):
    def test_identical_hash_in_different_round_is_counted_again(self):
        first = f"{ROOT}/2nd submission/00 1st Round of Comments/letter.docx"
        third = f"{ROOT}/4th submission/3rd Round of Comments/renamed-copy.docx"
        text = "Please provide the tree protection verification letter."
        dataset = {
            "comments": [
                row("C1", first, 1, text, "same-hash", document_date="May 12, 2025"),
                row("C3", third, 3, text, "same-hash", document_date="May 12, 2025"),
            ],
            "comment_response_links": [
                {"comment_id": "C1", "review_status": "confirmed", "response_id": "R1"},
            ],
        }

        report = mark_copied_source_documents(dataset, WORKSPACE)

        self.assertEqual(report["copied_source_groups"], 0)
        self.assertEqual(report["copied_source_paths_suppressed"], 0)
        self.assertEqual(report["copied_comment_rows_suppressed"], 0)
        self.assertTrue(dataset["comments"][0]["search_eligible"])
        self.assertTrue(dataset["comments"][1]["search_eligible"])
        self.assertEqual(dataset["comments"][0]["source_document_date"], "2025-05-12")

    def test_resaved_copy_requires_same_date_and_comment_set(self):
        first = f"{ROOT}/2nd submission/00 1st Round of Comments/letter.docx"
        third = f"{ROOT}/4th submission/3rd Round of Comments/renamed-copy.docx"
        text = "Please provide the tree protection verification letter."
        same_date = {
            "comments": [
                row("C1", first, 1, text, "hash-one", document_date="2025-05-12"),
                row("C3", third, 3, text, "hash-two", document_date="2025-05-12"),
            ],
            "comment_response_links": [],
        }
        report = mark_copied_source_documents(same_date, WORKSPACE)
        self.assertEqual(report["copied_source_groups"], 0)
        self.assertTrue(same_date["comments"][1]["search_eligible"])

        different_date = {
            "comments": [
                row("C1", first, 1, text, "hash-one", document_date="2025-05-12"),
                row("C3", third, 3, text, "hash-two", document_date="2025-06-12"),
            ],
            "comment_response_links": [],
        }
        report = mark_copied_source_documents(different_date, WORKSPACE)
        self.assertEqual(report["copied_source_groups"], 0)
        self.assertTrue(different_date["comments"][1]["search_eligible"])

    def test_named_differently_same_round_hash_is_still_one_source(self):
        first = f"{ROOT}/3rd submission/2nd Round of Comments/PC2-review.pdf"
        archive = f"{ROOT}/3rd submission/2nd Round of Comments/archive/PC2-review-copy.pdf"
        dataset = {
            "comments": [
                row("C1", first, 2, "Same complete row", "same-round-hash"),
                row("C2", archive, 2, "Same complete row", "same-round-hash"),
            ],
            "comment_response_links": [],
        }
        report = mark_copied_source_documents(dataset, WORKSPACE)
        self.assertEqual(report["copied_source_groups"], 1)
        self.assertFalse(dataset["comments"][1]["search_eligible"])

    def test_missing_date_does_not_suppress_different_hash(self):
        first = f"{ROOT}/2nd submission/00 1st Round of Comments/letter.docx"
        third = f"{ROOT}/4th submission/3rd Round of Comments/letter.docx"
        dataset = {
            "comments": [
                row("C1", first, 1, "Same text", "hash-one"),
                row("C3", third, 3, "Same text", "hash-two"),
            ],
            "comment_response_links": [],
        }
        report = mark_copied_source_documents(dataset, WORKSPACE)
        self.assertEqual(report["copied_source_groups"], 0)
        self.assertTrue(all(item["search_eligible"] for item in dataset["comments"]))


if __name__ == "__main__":
    unittest.main()
