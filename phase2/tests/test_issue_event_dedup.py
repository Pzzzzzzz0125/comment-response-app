import unittest

from phase2.issue_event_dedup import (
    assign_issue_threads,
    build_issue_event_index,
    event_identity,
    split_progression_events,
)


class IssueEventDedupTests(unittest.TestCase):
    def test_same_date_and_text_merge_with_source_occurrences(self):
        comments = [
            {
                "comment_id": "C1",
                "issue_thread_id": "T1",
                "source_document": "file-a.xlsx",
                "issue_thread_events": [{
                    "event_id": "E1",
                    "event_type": "reviewer_follow_up",
                    "occurred_at_label": "8/6/25 9:18 AM",
                    "exact_text": "Please show the sheet number.",
                }],
            },
            {
                "comment_id": "C2",
                "issue_thread_id": "T1",
                "source_document": "file-b.xlsx",
                "issue_thread_events": [{
                    "event_id": "E2",
                    "event_type": "reviewer_follow_up",
                    "occurred_at_label": "8/6/25 9:18 AM",
                    "exact_text": "  Please show the sheet number. ",
                }],
            },
        ]
        index = build_issue_event_index(comments)
        event = index["T1"]["events"][0]
        self.assertEqual(index["T1"]["canonical_event_count"], 1)
        self.assertEqual(event["merged_event_ids"], ["E1", "E2"])
        self.assertEqual(len(event["source_occurrences"]), 2)

    def test_role_family_and_markup_prefix_are_one_event(self):
        comments = [
            {
                "comment_id": "C1",
                "issue_thread_id": "T1",
                "review_round": "2",
                "source_document": "a.xlsx",
                "event_date": "2025-07-11",
                "verified_text": "Markup 25100917-STRC-PLANS.pdf BLDG REV-V2-C2 36 1. Provide the rack elevations.",
            },
            {
                "comment_id": "C2",
                "issue_thread_id": "T1",
                "review_round": "2",
                "source_document": "b.xlsx",
                "event_date": "2025-07-11",
                "verified_text": "1. Provide the rack elevations.",
                "issue_thread_events": [{
                    "event_id": "follow-up",
                    "event_type": "reviewer_follow_up",
                    "actor_role": "government",
                    "occurred_at_label": "7/11/25 3:50 PM",
                    "event_date": "2025-07-11",
                    "exact_text": "1. Provide the rack elevations.",
                }],
            },
        ]
        index = build_issue_event_index(comments)
        self.assertEqual(index["T1"]["canonical_event_count"], 1)
        self.assertEqual(len(index["T1"]["events"][0]["source_occurrences"]), 2)

    def test_cross_thread_copy_with_xml_escape_case_is_one_event(self):
        # An overlapping ``new/`` import can have a different legacy thread
        # and line-break escape casing even though it is the same visible row.
        comments = [
            {
                "comment_id": "C-old", "issue_thread_id": "T-old",
                "project_id": "PROJECT-1", "city": "San Jose",
                "discipline": "Building", "comment_number": "36",
                "review_round": "2", "text_trust_status": "verified",
                "verified_text": "Markup FORM.pdf BLDG REV-V2-C2 36 1. Provide rack elevations._x000d_ 2. Label each rack.",
                "source_document": "comments&response/site/old.xlsx",
            },
            {
                "comment_id": "C-new", "issue_thread_id": "T-new",
                "project_id": "PROJECT-1", "city": "San Jose",
                "discipline": "BPC WC3 1 : Building", "comment_number": "36",
                "review_round": "2", "text_trust_status": "verified",
                "verified_text": "Markup FORM.pdf BLDG REV-V2-C2 36 1. Provide rack elevations. 2. Label each rack.",
                "source_document": "new/site/new.xlsx",
            },
        ]
        grouping = assign_issue_threads(comments)
        self.assertEqual(grouping["multi_record_threads"], 1)
        self.assertEqual(comments[0]["issue_thread_id"], comments[1]["issue_thread_id"])
        timeline = build_issue_event_index(comments)[comments[0]["issue_thread_id"]]
        self.assertEqual(timeline["canonical_event_count"], 1)
        self.assertEqual(len(timeline["events"][0]["source_occurrences"]), 2)

    def test_different_dates_are_not_merged(self):
        left = {"event_type": "applicant_response", "occurred_at_label": "7/28/25", "exact_text": "Noted."}
        right = {"event_type": "applicant_response", "occurred_at_label": "9/11/25", "exact_text": "Noted."}
        self.assertNotEqual(event_identity(left), event_identity(right))

    def test_comment_event_date_participates_in_identity(self):
        left = {"event_type": "government_comment", "event_date": "2025-07-11", "exact_text": "Provide the detail."}
        right = {"event_type": "government_comment", "event_date": "2025-08-11", "exact_text": "Provide the detail."}
        self.assertNotEqual(event_identity(left), event_identity(right))

    def test_same_pc_marker_on_different_dates_is_not_merged(self):
        left = {
            "event_type": "government_comment", "event_round_marker": "PC1",
            "occurred_at_label": "05/04/2026", "exact_text": "Provide the detail.",
        }
        right = {
            "event_type": "government_comment", "event_round_marker": "PC1",
            "occurred_at_label": "06/04/2026", "exact_text": "Provide the detail.",
        }
        self.assertNotEqual(event_identity(left), event_identity(right))

    def test_printed_id_response_copies_on_different_dates_are_distinct(self):
        left = {
            "event_type": "applicant_response", "event_round_marker": "PC2",
            "printed_comment_id": "36", "occurred_at_label": "08/06/2025",
            "exact_text": "Noted. See sheet HPS-3.",
        }
        right = {
            "event_type": "applicant_response", "event_round_marker": "PC2",
            "printed_comment_id": "36", "occurred_at_label": "08/20/2025",
            "exact_text": "Noted. See sheet HPS-3.",
        }
        self.assertNotEqual(event_identity(left), event_identity(right))

    def test_printed_id_merges_pc1_copy_observed_in_later_document(self):
        left = {
            "event_type": "government_comment", "event_round_marker": "PC1",
            "printed_comment_id": "141", "occurred_at_label": "05/04/2026",
            "exact_text": "Provide the rated wall detail.",
        }
        right = {
            "event_type": "government_comment", "event_round_marker": "PC1",
            "printed_comment_id": "141", "occurred_at_label": "08/20/2026",
            "exact_text": "Provide the rated wall detail.",
        }
        self.assertEqual(event_identity(left), event_identity(right))

    def test_near_duplicate_is_review_only(self):
        comments = [
            {
                "comment_id": "C1", "issue_thread_id": "T-review",
                "review_round": "2", "comment_number": "7",
                "source_document": "a.xlsx",
                "verified_text": (
                    "Please provide the wall detail on Sheet A5 and identify the "
                    "rated assembly on the architectural plan."
                ),
            },
            {
                "comment_id": "C2", "issue_thread_id": "T-review",
                "review_round": "2", "comment_number": "7",
                "source_document": "b.xlsx",
                "verified_text": (
                    "Please provide the wall detail on Sheet A5 and identify the "
                    "rated assembly on the architectural plan. See note."
                ),
            },
        ]
        timeline = build_issue_event_index(comments)["T-review"]
        self.assertEqual(timeline["canonical_event_count"], 2)
        self.assertEqual(len(timeline["dedup_review_queue"]), 1)
        self.assertTrue(all(
            event["dedup_decision"] == "POSSIBLE_DUPLICATE"
            for event in timeline["events"]
        ))

    def test_missing_date_is_left_as_a_separate_occurrence(self):
        comments = [{
            "comment_id": "C1",
            "issue_thread_id": "T1",
            "source_document": "file-a.xlsx",
            "issue_thread_events": [{"event_id": "E1", "event_type": "discussion_note", "exact_text": "Noted."}],
        }, {
            "comment_id": "C2",
            "issue_thread_id": "T1",
            "source_document": "file-b.xlsx",
            "issue_thread_events": [{"event_id": "E2", "event_type": "discussion_note", "exact_text": "Noted."}],
        }]
        self.assertEqual(build_issue_event_index(comments)["T1"]["canonical_event_count"], 2)

    def test_same_text_in_different_rounds_has_unique_event_ids(self):
        comments = [{
            "comment_id": "C1", "issue_thread_id": "T1",
            "review_round": "1", "verified_text": "Provide the tree letter.",
            "source_document": "PC1.pdf",
        }, {
            "comment_id": "C2", "issue_thread_id": "T1",
            "review_round": "2", "verified_text": "Provide the tree letter.",
            "source_document": "PC2.pdf",
        }, {
            "comment_id": "C3", "issue_thread_id": "T1",
            "review_round": "3", "verified_text": "Provide the tree letter.",
            "source_document": "PC3.pdf",
        }]
        timeline = build_issue_event_index(comments)["T1"]
        self.assertEqual(timeline["canonical_event_count"], 3)
        self.assertEqual(
            [event["effective_round"] for event in timeline["events"]],
            ["1", "2", "3"],
        )
        event_ids = [event["event_id"] for event in timeline["events"]]
        self.assertEqual(len(event_ids), len(set(event_ids)))

    def test_same_round_copy_merges_but_keeps_both_sources(self):
        comments = [{
            "comment_id": "C1", "issue_thread_id": "T1",
            "review_round": "2", "verified_text": "Provide the tree letter.",
            "source_document": "copy-a.pdf",
        }, {
            "comment_id": "C2", "issue_thread_id": "T1",
            "review_round": "2", "verified_text": "Provide the tree letter.",
            "source_document": "copy-b.pdf",
        }]
        timeline = build_issue_event_index(comments)["T1"]
        self.assertEqual(timeline["canonical_event_count"], 1)
        self.assertEqual(len(timeline["events"][0]["source_occurrences"]), 2)

    def test_same_date_truncated_and_complete_response_merge(self):
        comments = [
            {
                "comment_id": "C-short", "issue_thread_id": "T-response",
                "source_document": "response-a.xlsx",
                "issue_thread_events": [{
                    "event_id": "R-short", "event_type": "applicant_response",
                    "actor_role": "company", "actor": "Applicant",
                    "occurred_at_label": "9/11/25 5:54 PM",
                    "exact_text": "This shearwall design for portal frame per CBC 2308.6.5.2",
                }],
            },
            {
                "comment_id": "C-full", "issue_thread_id": "T-response",
                "source_document": "response-b.xlsx",
                "issue_thread_events": [{
                    "event_id": "R-full", "event_type": "current_applicant_response",
                    "actor_role": "company", "actor": "Applicant",
                    "occurred_at_label": "9/11/25 5:54 PM",
                    "exact_text": "This shearwall design for portal frame per CBC 2308.6.5.2, see detail 3/SD1. See page 33 for calculations.",
                }],
            },
        ]
        timeline = build_issue_event_index(comments)["T-response"]
        self.assertEqual(timeline["canonical_event_count"], 1)
        self.assertEqual(timeline["events"][0]["dedup_decision"], "HIGH_CONFIDENCE_DUPLICATE")
        self.assertEqual(len(timeline["events"][0]["source_occurrences"]), 2)

    def test_same_response_date_changed_is_kept_as_reissue(self):
        comments = [
            {
                "comment_id": "C-a", "issue_thread_id": "T-response",
                "source_document": "response-a.xlsx",
                "issue_thread_events": [{
                    "event_id": "R-a", "event_type": "applicant_response",
                    "actor_role": "company", "occurred_at_label": "9/11/25 5:54 PM",
                    "exact_text": "See sheet A5.",
                }],
            },
            {
                "comment_id": "C-b", "issue_thread_id": "T-response",
                "source_document": "response-b.xlsx",
                "issue_thread_events": [{
                    "event_id": "R-b", "event_type": "applicant_response",
                    "actor_role": "company", "occurred_at_label": "9/30/25 4:13 PM",
                    "exact_text": "See sheet A5.",
                }],
            },
        ]
        self.assertEqual(build_issue_event_index(comments)["T-response"]["canonical_event_count"], 2)

    def test_cumulative_files_form_one_four_event_timeline(self):
        first = {
            "comment_id": "C1", "city": "Menlo Park",
            "property_project": "2311 Warner Range Ave",
            "discipline": "Building", "comment_number": "171",
            "review_round": "1", "text_trust_status": "verified",
            "verified_text": (
                "PC1- Provide the surveyor signature. "
                "PC2: Comment remains."
            ),
            "source_document": (
                "comments&response/site-a/building/file-1.pdf"
            ),
            "source_locator_json": {"pages": [1]},
        }
        second = {
            **first,
            "comment_id": "C2", "review_round": "4",
            "verified_text": (
                "PC1- Provide the surveyor signature. "
                "PC2: Comment remains. "
                "PC3: Add the signature to C1. "
                "PC4: Accepted."
            ),
            "source_document": (
                "comments&response/site-a/building/file-2.pdf"
            ),
            "source_locator_json": {"pages": [2]},
        }
        grouping = assign_issue_threads([first, second])
        self.assertEqual(grouping["multi_record_threads"], 1)
        self.assertEqual(first["issue_thread_id"], second["issue_thread_id"])
        timeline = build_issue_event_index([first, second])[first["issue_thread_id"]]
        self.assertEqual(timeline["canonical_event_count"], 4)
        self.assertEqual(len(timeline["events"][0]["source_occurrences"]), 2)
        self.assertEqual(len(timeline["events"][1]["source_occurrences"]), 2)
        self.assertEqual(len(timeline["events"][2]["source_occurrences"]), 1)

    def test_progression_parser_keeps_event_body_and_round(self):
        events = split_progression_events({
            "verified_text": (
                "A. PC1- Sheet C1: Provide the signature. "
                "PC2: Comment remains PC3 &PC4: Add it to sheet C1"
            )
        })
        self.assertEqual(
            [event["event_round_marker"] for event in events],
            ["PC1", "PC2", "PC3&PC4"],
        )
        self.assertEqual(events[0]["exact_text"], "Sheet C1: Provide the signature.")
        self.assertEqual(events[1]["exact_text"], "Comment remains")

    def test_progression_parser_removes_repeated_source_type_decorators(self):
        events = split_progression_events({
            "verified_text": (
                '(S) PC2: Please specify "approved hanger".\n\n'
                '(S) PC1: Please specify "approved hanger".'
            )
        })
        self.assertEqual(
            [(event["event_round_marker"], event["exact_text"]) for event in events],
            [
                ("PC2", 'Please specify "approved hanger".'),
                ("PC1", 'Please specify "approved hanger".'),
            ],
        )

    def test_same_comment_number_on_different_sites_does_not_group(self):
        first = {
            "comment_id": "C1", "city": "San Jose", "property_project": "1 Main St",
            "discipline": "Building", "comment_number": "1", "review_round": "1",
            "verified_text": "PC1: Provide a 3-foot door.", "text_trust_status": "verified",
            "source_document": "comments&response/site-a/file.pdf",
        }
        second = {
            **first, "comment_id": "C2", "property_project": "2 Main St",
            "source_document": "comments&response/site-b/file.pdf",
        }
        self.assertEqual(assign_issue_threads([first, second])["multi_record_threads"], 0)

    def test_same_issue_body_links_pc1_pc2_and_confirmed_response_copy(self):
        comments = [
            {
                "comment_id": "C-PC1", "city": "Menlo Park",
                "property_project": "2311 Warner Range Ave",
                "discipline": "Building", "comment_number": "93",
                "review_round": "1", "text_trust_status": "verified",
                "verified_text": '(S) PC1: Please specify "approved hanger".',
                "source_document": "comments&response/site/building/PC1-review.pdf",
            },
            {
                "comment_id": "C-PC2", "city": "Menlo Park",
                "property_project": "2311 Warner Range Ave",
                "discipline": "Building", "comment_number": "218",
                "review_round": "2", "text_trust_status": "verified",
                "verified_text": '(S) PC2: Please specify "approved hanger".',
                "source_document": "comments&response/site/building/PC2-review.pdf",
            },
            {
                "comment_id": "C-response", "city": "Menlo Park",
                "property_project": "25 001 2311 Warner Range Ave — Building",
                "discipline": "BPC WC3 1 : Building", "comment_number": "218",
                "review_round": "2", "source_status": "verified",
                "human_review_status": "confirmed",
                "original_text": '(S) PC2: Please specify "approved hanger".',
                "source_document": "comments&response/site/building/PC3 Response Letter.pdf",
            },
        ]
        grouping = assign_issue_threads(comments)
        self.assertEqual(grouping["multi_record_threads"], 1)
        self.assertEqual(len({row["issue_thread_id"] for row in comments}), 1)
        timeline = build_issue_event_index(comments)[comments[0]["issue_thread_id"]]
        self.assertEqual(timeline["canonical_event_count"], 2)
        self.assertEqual(
            [event["event_round_marker"] for event in timeline["events"]],
            ["PC1", "PC2"],
        )
        self.assertEqual(len(timeline["events"][1]["source_occurrences"]), 2)

    def test_canonical_pc_event_prefers_its_original_review_round(self):
        comments = [
            {
                "comment_id": "C-cumulative", "issue_thread_id": "T1",
                "review_round": "2", "text_trust_status": "verified",
                "verified_text": "PC2: Still open. PC1: Provide the signature.",
                "source_document": "PC2-review.pdf",
            },
            {
                "comment_id": "C-original", "issue_thread_id": "T1",
                "review_round": "1", "text_trust_status": "verified",
                "verified_text": "PC1: Provide the signature.",
                "source_document": "PC1-review.pdf",
            },
        ]
        timeline = build_issue_event_index(comments)["T1"]
        pc1 = next(
            event for event in timeline["events"]
            if event["event_round_marker"] == "PC1"
        )
        self.assertEqual(pc1["review_round"], "1")
        self.assertEqual(pc1["source_document"], "PC1-review.pdf")
        self.assertEqual(len(pc1["source_occurrences"]), 2)

    def test_cumulative_pc2_source_keeps_marker_and_document_round_separate(self):
        comment = {
            "comment_id": "C-cumulative", "issue_thread_id": "T1",
            "comment_number": "90", "review_round": "2",
            "document_round": "2", "text_trust_status": "verified",
            "verified_text": (
                "(S) PC2: Revise the stud bolt weld. "
                "(S) PC1: Specify the stud bolt end distance."
            ),
            "source_document": "PC2-review.pdf",
            "source_object_reference": "SD3",
        }
        timeline = build_issue_event_index([comment])["T1"]
        self.assertEqual(
            [event["effective_round"] for event in timeline["events"]],
            ["1", "2"],
        )
        self.assertTrue(all(
            event["review_round"] == "2"
            and event["observed_in_document_round"] == "2"
            for event in timeline["events"]
        ))
        occurrence = timeline["events"][0]["source_occurrences"][0]
        self.assertEqual(occurrence["printed_comment_id"], "90")
        self.assertEqual(occurrence["source_object_reference"], "SD3")

    def test_same_physical_row_keeps_distinct_pc_events_as_distinct_occurrences(self):
        comment = {
            "comment_id": "C-cumulative", "issue_thread_id": "T1",
            "comment_number": "90", "review_round": "2",
            "document_round": "2", "text_trust_status": "verified",
            "source_document": "PC2-review.pdf",
            "source_locator_json": {"pages": [3], "bounding_boxes": [{"page": 3}]},
            "verified_text": (
                "(S) PC2: Revise the stud bolt weld. "
                "(S) PC1: Specify the stud bolt end distance."
            ),
        }
        events = build_issue_event_index([comment])["T1"]["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            len({event["source_occurrence_ids"][0] for event in events}), 2
        )

    def test_inverse_dimensions_remain_distinct_issue_threads(self):
        comments = [
            {
                "comment_id": "C-12-to-6", "city": "Menlo Park",
                "property_project": "2311 Warner Range Ave",
                "discipline": "Building", "comment_number": "237",
                "review_round": "2", "text_trust_status": "verified",
                "verified_text": '(S) PC2: Please revise 12" to be 6" per detail 8/-.',
                "source_document": "comments&response/site/building/review.pdf",
            },
            {
                "comment_id": "C-6-to-12", "city": "Menlo Park",
                "property_project": "2311 Warner Range Ave",
                "discipline": "Building", "comment_number": "238",
                "review_round": "2", "text_trust_status": "verified",
                "verified_text": '(S) PC2: Please revise 6" to be 12" per detail 8/-.',
                "source_document": "comments&response/site/building/review.pdf",
            },
        ]
        self.assertEqual(assign_issue_threads(comments)["multi_record_threads"], 0)
        self.assertTrue(all(not row.get("issue_thread_id") for row in comments))

    def test_same_date_minor_ocr_variation_auto_merges(self):
        body_a = (
            "Please provide the approved wall assembly on Sheet A5 and "
            "coordinate the fire-rated detail with Section 1/A3.04."
        )
        body_b = (
            "Please provide approved wall assembly on sheet A5 and coordinate "
            "the fire rated detail with section 1/A3.04."
        )
        comments = [{
            "comment_id": "C1", "issue_thread_id": "T-ocr",
            "review_round": "2", "comment_number": "12",
            "source_document": "a.xlsx", "event_date": "2025-09-11",
            "verified_text": body_a,
        }, {
            "comment_id": "C2", "issue_thread_id": "T-ocr",
            "review_round": "2", "comment_number": "12",
            "source_document": "b.xlsx", "event_date": "2025-09-11",
            "verified_text": body_b,
        }]
        event = build_issue_event_index(comments)["T-ocr"]["events"][0]
        self.assertEqual(len(event["merged_event_ids"]), 2)
        self.assertEqual(event["dedup_decision"], "HIGH_CONFIDENCE_DUPLICATE")
        self.assertEqual(len(event["source_occurrences"]), 2)

    def test_labels_and_observation_submissions_are_unioned(self):
        comments = [{
            "comment_id": "C1", "issue_thread_id": "T-labels",
            "review_round": "2", "comment_number": "48",
            "source_document": "submission-3.xlsx",
            "issue_thread_events": [{
                "event_id": "R1", "event_type": "applicant_response",
                "actor_role": "company", "occurred_at_label": "9/11/2025",
                "event_round_marker": "PC2", "record_label": "Markup V2-C2 48",
                "submission": "3rd submission", "exact_text": "See Sheet SD1.",
            }],
        }, {
            "comment_id": "C2", "issue_thread_id": "T-labels",
            "review_round": "2", "comment_number": "48",
            "source_document": "submission-4.xlsx",
            "issue_thread_events": [{
                "event_id": "R2", "event_type": "current_applicant_response",
                "actor_role": "company", "occurred_at_label": "9/11/2025",
                "event_round_marker": "PC2", "record_label": "Response row 48",
                "submission": "4th submission", "exact_text": "See Sheet SD1.",
            }],
        }]
        event = build_issue_event_index(comments)["T-labels"]["events"][0]
        self.assertEqual(event["event_submission"], "3")
        self.assertEqual(event["observed_in_submissions"], ["3", "4"])
        self.assertEqual(
            set(event["event_labels"]),
            {"Markup V2-C2 48", "PC2", "Response row 48"},
        )

    def test_missing_actor_inherits_but_conflicting_actors_do_not_merge(self):
        base = {
            "event_type": "reviewer_follow_up", "actor_role": "government",
            "occurred_at_label": "8/20/2025",
            "exact_text": "Please revise the rated wall detail on Sheet A5.",
        }
        comments = [{
            "comment_id": "C1", "issue_thread_id": "T-actor",
            "comment_number": "8", "source_document": "a.xlsx",
            "issue_thread_events": [{"event_id": "E1", **base}],
        }, {
            "comment_id": "C2", "issue_thread_id": "T-actor",
            "comment_number": "8", "source_document": "b.xlsx",
            "issue_thread_events": [{"event_id": "E2", "actor": "Kia Goudarzi", **base}],
        }]
        event = build_issue_event_index(comments)["T-actor"]["events"][0]
        self.assertEqual(event["actor"], "Kia Goudarzi")

        comments.append({
            "comment_id": "C3", "issue_thread_id": "T-actor",
            "comment_number": "8", "source_document": "c.xlsx",
            "issue_thread_events": [{"event_id": "E3", "actor": "Other Reviewer", **base}],
        })
        self.assertEqual(
            build_issue_event_index(comments)["T-actor"]["canonical_event_count"], 2,
        )

    def test_changed_dimension_and_negation_remain_distinct(self):
        comments = []
        for event_id, text in (
            ("E1", 'Revise the opening from 12" to 6".'),
            ("E2", 'Revise the opening from 6" to 12".'),
            ("E3", 'Do not revise the opening from 12" to 6".'),
        ):
            comments.append({
                "comment_id": event_id, "issue_thread_id": "T-params",
                "comment_number": "4", "source_document": f"{event_id}.xlsx",
                "issue_thread_events": [{
                    "event_id": event_id, "event_type": "reviewer_follow_up",
                    "occurred_at_label": "8/20/2025", "exact_text": text,
                }],
            })
        self.assertEqual(
            build_issue_event_index(comments)["T-params"]["canonical_event_count"], 3,
        )

    def test_generic_response_requires_shared_parent_context(self):
        def response(comment_id, printed_id, source):
            return {
                "comment_id": comment_id, "issue_thread_id": "T-generic",
                "comment_number": printed_id, "source_document": source,
                "issue_thread_events": [{
                    "event_id": f"R-{comment_id}", "event_type": "applicant_response",
                    "occurred_at_label": "9/11/2025", "exact_text": "Noted.",
                }],
            }
        merged = build_issue_event_index([
            response("C1", "9", "a.xlsx"), response("C2", "9", "b.xlsx"),
        ])["T-generic"]
        self.assertEqual(merged["canonical_event_count"], 1)
        separate = build_issue_event_index([
            response("C1", "9", "a.xlsx"), response("C2", "10", "b.xlsx"),
        ])["T-generic"]
        self.assertEqual(separate["canonical_event_count"], 2)
        self.assertEqual(len(separate["dedup_review_queue"]), 1)

    def test_dated_copy_absorbs_undated_copy_with_same_round(self):
        comments = [{
            "comment_id": "C1", "issue_thread_id": "T-date",
            "review_round": "2", "comment_number": "21", "source_document": "a.xlsx",
            "issue_thread_events": [{
                "event_id": "E1", "event_type": "reviewer_follow_up",
                "occurred_at_label": "8/20/2025",
                "exact_text": "Provide the complete shear transfer detail at the ledger.",
            }],
        }, {
            "comment_id": "C2", "issue_thread_id": "T-date",
            "review_round": "2", "comment_number": "21", "source_document": "b.xlsx",
            "issue_thread_events": [{
                "event_id": "E2", "event_type": "government_comment",
                "exact_text": "Provide the complete shear transfer detail at the ledger.",
            }],
        }]
        event = build_issue_event_index(comments)["T-date"]["events"][0]
        self.assertEqual(len(event["merged_event_ids"]), 2)
        self.assertTrue(event.get("occurred_at_label") or event.get("event_date"))

    def test_same_cell_parser_variants_create_one_source_occurrence(self):
        comments = [{
            "comment_id": "C1", "issue_thread_id": "T-cell",
            "review_round": "2", "comment_number": "6",
            "source_document": "review.xlsx",
            "source_locator_json": {"sheet_name": "Review Comments", "cell_range": "C6"},
            "event_date": "2025-08-20",
            "verified_text": "Provide the rated-wall detail on Sheet A5.",
        }, {
            "comment_id": "C2", "issue_thread_id": "T-cell",
            "review_round": "2", "comment_number": "6",
            "source_document": "review.xlsx",
            "source_locator_json": {"sheet_name": "Review Comments", "cell_range": "C6"},
            "event_date": "2025-08-20",
            "verified_text": "Provide the rated wall detail on sheet A5.",
        }]
        event = build_issue_event_index(comments)["T-cell"]["events"][0]
        self.assertEqual(len(event["source_occurrences"]), 1)

    def test_response_only_source_row_joins_unique_dated_event_thread(self):
        comments = [{
            "comment_id": "C-parent", "city": "San Jose",
            "property_project": "365 Nature Ct", "discipline": "Building",
            "issue_thread_id": "T-parent",
            "comment_number": "48", "verified_text": "Provide FTAO analysis.",
            "text_trust_status": "verified", "source_document": "review.xlsx",
            "issue_thread_events": [{
                "event_id": "F1", "event_type": "reviewer_follow_up",
                "actor": "Gregg Schwartz", "occurred_at_label": "9/23/2025",
                "exact_text": "Provide a shear wall design compliant with NDS requirements.",
            }],
        }, {
            "comment_id": "C-event-only", "city": "San Jose",
            "property_project": "365 Nature Ct",
            "discipline": "Building", "verified_text": "",
            "text_trust_status": "verified", "source_document": "response.pdf",
            "issue_thread_id": "T-legacy-separate",
            "issue_thread_events": [{
                "event_id": "F2", "event_type": "government_comment",
                "actor": "Gregg Schwartz", "occurred_at_label": "9/23/2025",
                "exact_text": "Provide a shear wall design compliant with NDS requirements.",
            }],
        }]
        grouping = assign_issue_threads(comments)
        self.assertEqual(grouping["event_alias_rows_grouped"], 1)
        self.assertEqual(comments[0]["issue_thread_id"], comments[1]["issue_thread_id"])
        timeline = build_issue_event_index(comments)[comments[0]["issue_thread_id"]]
        self.assertEqual(timeline["canonical_event_count"], 2)
        follow_up = next(
            event for event in timeline["events"]
            if "shear wall design" in event.get("exact_text", "")
        )
        self.assertEqual(len(follow_up["source_occurrences"]), 2)

    def test_undated_source_only_text_inherits_unique_trusted_event_thread(self):
        text = (
            "Provide a shear wall design compliant with NDS requirements "
            "for the two-story building."
        )
        comments = [{
            "comment_id": "trusted", "project_id": "P1", "city": "San Jose",
            "verified_text": "Main structural review comment.",
            "text_trust_status": "verified", "issue_thread_id": "T-main",
            "issue_thread_events": [{
                "event_id": "followup", "event_type": "reviewer_follow_up",
                "actor": "Reviewer One", "event_date": "2025-09-23",
                "exact_text": text,
            }],
        }, {
            "comment_id": "source-only", "project_id": "P1", "city": "San Jose",
            "original_text": text, "reviewer": "Reviewer One",
            "text_trust_status": "quarantined", "issue_thread_id": "T-legacy",
            "source_document": "copied-history.pdf",
            "source_locator_json": {"page": 4},
        }]

        stats = assign_issue_threads(comments)
        self.assertEqual(stats["event_alias_rows_grouped"], 1)
        self.assertEqual(comments[1]["issue_thread_id"], "T-main")
        self.assertEqual(
            comments[1]["issue_grouping_method"],
            "same_site_role_exact_event_inherited_date",
        )
        index = build_issue_event_index(comments)
        matching = [
            event for event in index["T-main"]["events"]
            if "shear wall design" in event.get("normalized_text", "")
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(len(matching[0]["source_occurrences"]), 2)

    def test_same_event_across_pdf_xlsx_and_docx_is_one_event(self):
        comments = []
        for suffix in ("pdf", "xlsx", "docx"):
            comments.append({
                "comment_id": f"C-{suffix}", "issue_thread_id": "T1",
                "source_document": f"review-copy.{suffix}",
                "issue_thread_events": [{
                    "event_id": f"E-{suffix}",
                    "event_type": "reviewer_follow_up",
                    "actor": "Reviewer One", "event_date": "2025-08-20",
                    "exact_text": "Revise the rated wall detail on Sheet A5.",
                }],
            })

        index = build_issue_event_index(comments)
        self.assertEqual(index["T1"]["canonical_event_count"], 1)
        event = index["T1"]["events"][0]
        self.assertEqual(len(event["source_occurrences"]), 3)
        self.assertEqual(
            {item["source_document"] for item in event["source_occurrences"]},
            {"review-copy.pdf", "review-copy.xlsx", "review-copy.docx"},
        )
