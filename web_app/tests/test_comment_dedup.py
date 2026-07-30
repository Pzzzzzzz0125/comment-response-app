import unittest

from web_app.comment_dedup import find_duplicate_comments, mark_duplicate_comments


class CommentDedupTests(unittest.TestCase):
    def test_same_file_and_normalized_text_are_returned_once(self):
        comments = [
            {"comment_id": "C-1", "original_text": "Show the door width.\n", "source_sha256": "abc", "source_row": 4,
             "response_id": "R-1", "human_review_status": "confirmed", "text_trust_status": "verified"},
            {"comment_id": "C-2", "original_text": " Show  the door width. ", "source_sha256": "abc", "source_row": 9},
            {"comment_id": "C-3", "original_text": "Show the door width.", "source_sha256": "different", "source_row": 4},
        ]
        links = [{"comment_id": "C-1", "response_id": "R-1", "review_status": "confirmed"}]
        canonical, duplicate_of = find_duplicate_comments(comments, links)
        self.assertEqual({row["comment_id"] for row in canonical}, {"C-1", "C-3"})
        self.assertEqual(duplicate_of, {"C-2": "C-1"})

    def test_marking_preserves_duplicate_record_for_audit(self):
        dataset = {"comments": [
            {"comment_id": "C-1", "verified_text": "Same", "source_document": "a.docx", "source_row": 1},
            {"comment_id": "C-2", "verified_text": "Same", "source_document": "a.docx", "source_row": 2},
        ], "comment_response_links": []}
        report = mark_duplicate_comments(dataset)
        self.assertEqual(report["duplicate_rows_suppressed"], 1)
        self.assertTrue(dataset["comments"][0].get("search_eligible", True))
        self.assertFalse(dataset["comments"][1]["search_eligible"])
        self.assertEqual(dataset["comments"][1]["duplicate_of"], "C-1")

    def test_same_site_round_text_is_one_comment_across_different_files(self):
        comments = [
            {"comment_id": "C-1", "verified_text": "Provide the soils report.", "city": "Menlo Park",
             "property_project": "2311 Warner Range Ave", "review_round": "2", "source_document": "a.pdf"},
            {"comment_id": "C-2", "verified_text": " Provide  the soils report. ", "city": "Menlo Park",
             "property_project": "2311 Warner Range Ave", "reviewed_plan_round": "Review round 2",
             "source_document": "b.pdf"},
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(duplicate_of, {"C-2": "C-1"})
        self.assertEqual(canonical[0]["duplicate_source_documents"], ["a.pdf", "b.pdf"])

    def test_same_text_counts_again_for_different_round_or_site(self):
        comments = [
            {"comment_id": "C-1", "verified_text": "Provide the soils report.", "city": "Menlo Park",
             "property_project": "2311 Warner Range Ave", "review_round": "2", "source_document": "a.pdf"},
            {"comment_id": "C-2", "verified_text": "Provide the soils report.", "city": "Menlo Park",
             "property_project": "2311 Warner Range Ave", "review_round": "3", "source_document": "b.pdf"},
            {"comment_id": "C-3", "verified_text": "Provide the soils report.", "city": "Menlo Park",
             "property_project": "10 Other St", "review_round": "2", "source_document": "c.pdf"},
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual({row["comment_id"] for row in canonical}, {"C-1", "C-2", "C-3"})
        self.assertEqual(duplicate_of, {})

    def test_extraction_filler_variant_is_duplicate_but_parameter_change_is_not(self):
        comments = [
            {"comment_id": "C-240", "verified_text": "(S) PC2: Please provide A35 clip blocking to mudsill.",
             "city": "Menlo Park", "property_project": "2311 Warner Range Ave", "review_round": "2",
             "source_document": "comments&response/project/building/a.pdf"},
            {"comment_id": "C-245", "verified_text": "(S) PC2: Please provide A35 clip at blocking to mudsill.",
             "city": "Menlo Park", "property_project": "Different label for same address", "review_round": "2",
             "source_document": "comments&response/project/building/b.pdf"},
            {"comment_id": "C-door-3", "verified_text": "The door width shall be 3 feet.",
             "city": "Menlo Park", "property_project": "2311 Warner Range Ave", "review_round": "2",
             "source_document": "comments&response/project/building/c.pdf"},
            {"comment_id": "C-door-4", "verified_text": "The door width shall be 4 feet.",
             "city": "Menlo Park", "property_project": "2311 Warner Range Ave", "review_round": "2",
             "source_document": "comments&response/project/building/d.pdf"},
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual(duplicate_of, {"C-245": "C-240"})
        self.assertEqual(
            {row["comment_id"] for row in canonical},
            {"C-240", "C-door-3", "C-door-4"},
        )

    def test_same_printed_form_row_with_context_is_one_comment(self):
        comments = [
            {"comment_id": "C-form", "verified_text": "(A) PC2- Sheet A2.00: Please clarify this object in the M. bedroom Bath",
             "city": "Menlo Park", "property_project": "2311 Warner Range Ave", "review_round": "2",
             "discipline": "Building", "comment_number": "186",
             "source_document": "comments&response/project/PC2-plan-review.pdf"},
            {"comment_id": "C-response", "verified_text": "(A) PC2- Sheet A2.00: Please clarify this object in the M. bedroom",
             "city": "Menlo Park", "property_project": "2311 Warner Range Ave", "review_round": "2",
             "discipline": "Building", "comment_number": "186",
             "source_document": "comments&response/project/PC3-response-letter.pdf"},
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual({row["comment_id"] for row in canonical}, {"C-form"})
        self.assertEqual(duplicate_of, {"C-response": "C-form"})

    def test_same_printed_number_different_parameter_is_not_merged(self):
        comments = [
            {"comment_id": "C-door-3", "verified_text": "The door width shall be 3 feet.",
             "city": "Menlo Park", "property_project": "2311 Warner Range Ave", "review_round": "2",
             "discipline": "Building", "comment_number": "12", "source_document": "comments&response/project/a.pdf"},
            {"comment_id": "C-door-4", "verified_text": "The door width shall be 4 feet.",
             "city": "Menlo Park", "property_project": "2311 Warner Range Ave", "review_round": "2",
             "discipline": "Building", "comment_number": "12", "source_document": "comments&response/project/b.pdf"},
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual({row["comment_id"] for row in canonical}, {"C-door-3", "C-door-4"})
        self.assertEqual(duplicate_of, {})

    def test_repeated_complete_hierarchy_is_one_per_site_round(self):
        shared_tail = (
            "\na. Please work with the project team on design changes."
            "\nb. Approval requires mitigation before permit approval."
        )
        comments = [
            {
                "comment_id": "C-round-1-a",
                "verified_text": "Impacts to trees #414 and 418 are severe." + shared_tail,
                "hierarchy_status": "merged_parent",
                "city": "Menlo Park", "property_project": "2311 Warner Range Ave",
                "review_round": "1", "source_document": "comments&response/project/a.docx",
                "response_id": "R-1",
            },
            {
                "comment_id": "C-round-1-b",
                "verified_text": (
                    "Impacts to trees #414 and 418 (these are labeled T4 and T6 "
                    "in the plan set) are severe." + shared_tail
                ),
                "hierarchy_status": "merged_parent",
                "city": "Menlo Park", "property_project": "2311 Warner Range Ave",
                "review_round": "1", "source_document": "comments&response/project/b.docx",
                "response_id": "R-2",
            },
            {
                "comment_id": "C-round-2",
                "verified_text": "Impacts to trees #414 and 418 are severe." + shared_tail,
                "hierarchy_status": "merged_parent",
                "city": "Menlo Park", "property_project": "2311 Warner Range Ave",
                "review_round": "2", "source_document": "comments&response/project/c.docx",
            },
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual(
            {row["comment_id"] for row in canonical},
            {"C-round-1-a", "C-round-2"},
        )
        self.assertEqual(duplicate_of, {"C-round-1-b": "C-round-1-a"})
        self.assertEqual(
            comments[0]["duplicate_response_ids"], ["R-1", "R-2"]
        )


if __name__ == "__main__":
    unittest.main()
