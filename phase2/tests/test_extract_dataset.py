import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from corpus_audit import audit_corpus as audit
from phase2 import extract_dataset as phase2

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "corpus_audit" / "tests"))
from test_audit_corpus import make_xlsx


def inventory_record(path: Path, response: bool) -> dict:
    return {
        "path": path.as_posix(),
        "sha256": "a" * 64,
        "likely_city": "San Jose",
        "likely_property_project": "Test Project",
        "likely_review_round": "1",
        "primary_sheet": "Review Matrix",
        "likely_comment_columns": {"Review Matrix": [{"column": "B", "header": "City Comment"}]},
        "likely_response_columns": {"Review Matrix": ([{"column": "C", "header": "Applicant Response"}] if response else [])},
        "detected_spreadsheet_headers": {
            "Review Matrix": {
                "row": 1,
                "columns": [
                    {"column": "A", "header": "Ref #"},
                    {"column": "B", "header": "City Comment"},
                    {"column": "C", "header": "Applicant Response"},
                ],
            }
        },
    }


class Phase2Tests(unittest.TestCase):
    def test_comment_only_row_has_explicit_unmatched_link(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "comments.xlsx"
            make_xlsx(path, ["Ref #", "City Comment", "Applicant Response"], [["1", "Revise the plan.", ""]])
            comments, responses, links, summary, review = phase2.extract_spreadsheet(path, inventory_record(path, False))
            self.assertEqual(len(comments), 1)
            self.assertEqual(responses, [])
            self.assertEqual(links[0]["response_id"], "")
            self.assertEqual(links[0]["match_status"], "unmatched")
            self.assertEqual(summary["unmatched_count"], 1)

    def test_same_row_response_creates_suggested_match(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "combined.xlsx"
            make_xlsx(path, ["Ref #", "City Comment", "Applicant Response"], [["7", "Revise the plan.", "Sheet A1 revised."]])
            comments, responses, links, summary, review = phase2.extract_spreadsheet(path, inventory_record(path, True))
            self.assertEqual(len(comments), len(responses), len(links))
            self.assertEqual(comments[0]["response_id"], responses[0]["response_id"])
            self.assertEqual(links[0]["matching_method"], "same_spreadsheet_row")
            self.assertEqual(links[0]["review_status"], "suggested")
            self.assertEqual(len(review), 1)

    def test_pdf_parser_preserves_numbered_items_and_pages(self):
        pages = [
            "Header only",
            "1 | General First comment line.\ncontinued first line\n2 | A2.1 Second comment.",
            "continued second line\n3 | Structural Third comment.\nPlease resubmit complete sets.",
        ]
        items = phase2.parse_numbered_pdf_comments(pages)
        self.assertEqual([item["number"] for item in items], ["1", "2", "3"])
        self.assertEqual(items[1]["page"], 2)
        self.assertEqual(items[1]["page_end"], 3)
        self.assertNotIn("Please resubmit", items[2]["text"])

    def test_dataset_validation_requires_one_link_per_comment(self):
        comments = [{"comment_id": "C-1"}]
        with self.assertRaises(ValueError):
            phase2.validate_dataset(comments, [], [])

    def test_ids_are_deterministic(self):
        self.assertEqual(
            phase2.stable_id("C", "source", "sheet", 4),
            phase2.stable_id("C", "source", "sheet", 4),
        )

    def test_generic_reviewer_label_splits_discipline_and_name(self):
        discipline, reviewer = phase2.split_reviewer(
            "Grading & Drainage Plan Review William Teav 5/29/26 4:12 PM"
        )
        self.assertEqual(discipline, "Grading & Drainage Plan Review")
        self.assertEqual(reviewer, "William Teav")

    def test_confirmation_updates_reviewed_items_but_not_unmatched_links(self):
        comments = [
            {"comment_id": "C-1", "human_review_status": "pending"},
            {"comment_id": "C-2", "human_review_status": "not_required"},
        ]
        responses = [{"response_id": "R-1", "human_review_status": "pending"}]
        links = [
            {"link_id": "L-1", "comment_id": "C-1", "response_id": "R-1", "review_status": "suggested"},
            {"link_id": "L-2", "comment_id": "C-2", "response_id": "", "review_status": "not_applicable"},
        ]
        review = [{
            "item_type": "comment_response_link",
            "item_id": "L-1",
            "source_document": "data/combined.xlsx",
        }]
        phase2.apply_review_decision(
            comments, responses, links, review,
            [{
                "scope_type": "source_path_prefix",
                "scope_value": "data/",
                "decision": "confirmed",
                "note": "Confirmed by user.",
            }],
        )
        self.assertEqual(links[0]["review_status"], "confirmed")
        self.assertEqual(comments[0]["human_review_status"], "confirmed")
        self.assertEqual(responses[0]["human_review_status"], "confirmed")
        self.assertEqual(links[1]["review_status"], "not_applicable")
        self.assertEqual(comments[1]["human_review_status"], "not_required")


if __name__ == "__main__":
    unittest.main()
