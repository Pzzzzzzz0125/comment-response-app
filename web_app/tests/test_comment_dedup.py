import unittest

from web_app.comment_dedup import (
    deduplicate_responses,
    event_date_key,
    find_duplicate_comments,
    mark_duplicate_comments,
)


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

    def test_xml_escape_case_is_not_a_second_comment(self):
        comments = [
            {"comment_id": "C-1", "verified_text": "Provide the rack._x000d_ 2. Label it.",
             "city": "San Jose", "project_id": "PROJECT-1", "review_round": "2", "source_document": "a.xlsx"},
            {"comment_id": "C-2", "verified_text": "Provide the rack. 2. Label it.",
             "city": "San Jose", "project_id": "PROJECT-1", "review_round": "2", "source_document": "b.xlsx"},
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(duplicate_of, {"C-2": "C-1"})

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

    def test_later_submission_is_kept_for_issue_timeline(self):
        comments = [
            {
                "comment_id": "C-submission-3",
                "verified_text": "Provide the soils report.",
                "city": "San Jose",
                "property_project": "365 Nature",
                "review_round": "1",
                "source_document": "comments&response/365/3rd submission/review.xlsx",
            },
            {
                "comment_id": "C-submission-4",
                "verified_text": "Provide the soils report.",
                "city": "San Jose",
                "property_project": "365 Nature",
                "review_round": "1",
                "source_document": "comments&response/365/4th submission/review.xlsx",
            },
            {
                "comment_id": "C-submission-4-copy",
                "verified_text": "Provide the soils report.",
                "city": "San Jose",
                "property_project": "365 Nature",
                "review_round": "1",
                "source_document": "comments&response/365/4th submission/archive/review-copy.xlsx",
            },
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual(
            {row["comment_id"] for row in canonical},
            {"C-submission-3", "C-submission-4"},
        )
        self.assertEqual(duplicate_of, {"C-submission-4-copy": "C-submission-4"})

    def test_same_dated_event_across_submissions_merges_and_keeps_occurrences(self):
        comments = [
            {
                "comment_id": "C-submission-3",
                "verified_text": "Provide the soils report.",
                "city": "San Jose",
                "property_project": "365 Nature",
                "review_round": "1",
                "reviewer": "Reviewer 6/17/25 2:41 PM",
                "source_document": "comments&response/365/3rd submission/review.xlsx",
                "source_row": 4,
            },
            {
                "comment_id": "C-submission-4",
                "verified_text": "Provide the soils report.",
                "city": "San Jose",
                "property_project": "365 Nature",
                "review_round": "1",
                "reviewer": "Reviewer 6/17/25 2:41 PM",
                "source_document": "comments&response/365/4th submission/review.xlsx",
                "source_row": 4,
                "response_id": "R-submission-4",
            },
        ]
        dataset = {
            "comments": comments,
            "responses": [{
                "response_id": "R-submission-4",
                "original_text": "See the soils report.",
                "source_document": comments[1]["source_document"],
                "source_row": 4,
            }],
            "comment_response_links": [],
        }
        self.assertEqual(event_date_key(comments[0]), "2025-06-17")
        report = mark_duplicate_comments(dataset)
        self.assertEqual(report["duplicate_rows_suppressed"], 1)
        winner = next(row for row in comments if row.get("search_eligible", True))
        loser = next(row for row in comments if row is not winner)
        self.assertTrue(winner.get("source_occurrences"))
        self.assertEqual(loser["duplicate_of"], winner["comment_id"])

    def test_same_date_response_copies_merge_and_repoint_parent(self):
        dataset = {
            "comments": [{
                "comment_id": "C-1", "response_id": "R-1",
                "city": "San Jose", "project_id": "P-1", "review_round": "2",
            }],
            "responses": [
                {
                    "response_id": "R-1", "comment_id": "C-1",
                    "verified_text": "Response: See sheet A5.",
                    "event_date": "2025-08-06", "review_round": "2",
                    "source_document": "one.xlsx", "source_row": 4,
                },
                {
                    "response_id": "R-2", "comment_id": "C-1",
                    "verified_text": "RESPONSE_See sheet A5.",
                    "event_date": "2025-08-06", "review_round": "2",
                    "source_document": "two.xlsx", "source_row": 4,
                },
            ],
            "comment_response_links": [{
                "comment_id": "C-1", "response_id": "R-2",
            }],
        }
        report = deduplicate_responses(dataset)
        self.assertEqual(report["duplicate_response_rows_suppressed"], 1)
        self.assertEqual(dataset["comments"][0]["response_id"], "R-1")
        self.assertEqual(dataset["comment_response_links"][0]["response_id"], "R-1")
        loser = next(row for row in dataset["responses"] if row["response_id"] == "R-2")
        self.assertFalse(loser["search_eligible"])
        self.assertEqual(loser["duplicate_of"], "R-1")
        winner = next(row for row in dataset["responses"] if row["response_id"] == "R-1")
        self.assertEqual(len(winner["source_occurrences"]), 1)

    def test_response_copies_on_different_dates_remain_distinct(self):
        dataset = {
            "comments": [{"comment_id": "C-1"}],
            "responses": [
                {"response_id": "R-1", "comment_id": "C-1", "verified_text": "Noted.",
                 "event_date": "2025-08-06", "review_round": "2"},
                {"response_id": "R-2", "comment_id": "C-1", "verified_text": "Noted.",
                 "event_date": "2025-08-20", "review_round": "2"},
            ],
            "comment_response_links": [],
        }
        report = deduplicate_responses(dataset)
        self.assertEqual(report["duplicate_response_rows_suppressed"], 0)
        self.assertNotIn("duplicate_of", dataset["responses"][0])
        self.assertNotIn("duplicate_of", dataset["responses"][1])

    def test_different_event_dates_do_not_merge_even_when_text_matches(self):
        comments = [
            {
                "comment_id": "C-june",
                "verified_text": "Provide the soils report.",
                "city": "San Jose",
                "property_project": "365 Nature",
                "review_round": "1",
                "reviewer": "Reviewer 6/17/25 2:41 PM",
                "source_document": "comments&response/365/3rd submission/review.xlsx",
            },
            {
                "comment_id": "C-july",
                "verified_text": "Provide the soils report.",
                "city": "San Jose",
                "property_project": "365 Nature",
                "review_round": "1",
                "reviewer": "Reviewer 7/17/25 2:41 PM",
                "source_document": "comments&response/365/4th submission/review.xlsx",
            },
        ]
        canonical, duplicate_of = find_duplicate_comments(comments)
        self.assertEqual(len(canonical), 2)
        self.assertEqual(duplicate_of, {})

    def test_response_letter_date_does_not_split_repeated_government_comment(self):
        review = {
            "comment_id": "C-review", "verified_text": "(S) PC2: Specify an approved hanger.",
            "text_trust_status": "verified", "city": "Menlo Park",
            "property_project": "2311 Warner Range Ave", "review_round": "2",
            "source_document": "comments&response/site/building/3rd submission/PC2-review.pdf",
        }
        response_copy = {
            "comment_id": "C-response", "original_text": "(S) PC2: Specify an approved hanger.",
            "city": "Menlo Park", "property_project": "2311 Warner Range Ave",
            "review_round": "2", "response_id": "R-response",
            "human_review_status": "confirmed", "source_document_date": "2025-11-24",
            "source_document": "comments&response/site/building/3rd submission/PC3 Response Letter.pdf",
        }
        links = [{
            "comment_id": "C-response", "response_id": "R-response",
            "review_status": "confirmed",
        }]
        self.assertEqual(event_date_key(response_copy), "")
        canonical, duplicate_of = find_duplicate_comments([review, response_copy], links)
        self.assertEqual([row["comment_id"] for row in canonical], ["C-response"])
        self.assertEqual(duplicate_of, {"C-review": "C-response"})

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
