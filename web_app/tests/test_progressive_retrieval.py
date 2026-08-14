import copy
import unittest

from web_app.progressive_retrieval import (
    ValidatedTagIndex,
    progressive_retrieve,
)


def row(
    comment_id,
    text,
    *,
    project="Project A",
    event_id=None,
    issue_id=None,
    issue_tags=None,
    event_tags=None,
    response=False,
    verification_status="confirmed",
    tag_status=None,
):
    tags = issue_tags
    if tag_status and tags:
        tags = [{"tag_id": tag, "status": tag_status} for tag in tags]
    value = {
        "comment_id": comment_id,
        "canonical_event_id": event_id or f"event-{comment_id}",
        "canonical_issue_id": issue_id or f"issue-{project}",
        "project_id": project,
        "city": "San Jose",
        "verified_text": text,
        "original_text": text,
        "verification_status": verification_status,
        "search_eligible": True,
        "issue_tags": tags or [],
        "event_tags": event_tags or [],
    }
    if response:
        value.update({"response_id": f"response-{comment_id}", "match_status": "confirmed"})
    if tag_status:
        value["tag_status"] = tag_status
    return value


class ProgressiveRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.fire_a = row(
            "fire-a", "Provide a one-hour rated wall between the ADU and main dwelling.",
            project="Project A", issue_tags=["fire_separation"], event_tags=["rated_wall"], response=True,
        )
        self.fire_b = row(
            "fire-b", "Protect the opening in the property-line wall with an approved rated assembly.",
            project="Project B", issue_tags=["fire_separation"], event_tags=["opening_protection"], response=True,
        )
        self.grading = row(
            "grading", "Provide a grading and drainage site plan for the proposed runoff.",
            project="Project C", issue_tags=["drainage"], event_tags=["grading"], response=True,
        )

    def test_issue_level_exact_tag_retrieval(self):
        result = progressive_retrieve(
            "fire separation", [self.fire_a, self.fire_b], intent="compare_groups",
        )
        self.assertEqual(result.stage, 1)
        self.assertEqual(result.coverage["project_count"], 2)
        self.assertEqual({item["comment_id"] for item in result.rows}, {"fire-a", "fire-b"})

    def test_event_level_exact_tag_retrieval(self):
        result = progressive_retrieve("rated wall", [self.fire_a, self.fire_b])
        self.assertEqual(result.stage, 1)
        self.assertEqual([item["comment_id"] for item in result.rows], ["fire-a"])

    def test_controlled_related_tag_expansion(self):
        base = row(
            "base", "Fire separation requirement.", project="Project A",
            issue_tags=["fire_separation"], response=True,
        )
        related = row(
            "related", "Provide an approved opening protection assembly.", project="Project B",
            event_tags=["opening_protection"], response=True,
        )
        result = progressive_retrieve("fire separation", [base, related], intent="compare_groups")
        self.assertEqual(result.stage, 2)
        self.assertIn("related", {item["comment_id"] for item in result.rows})

    def test_unconfirmed_tag_cannot_enter_stage_one(self):
        probable = row(
            "probable", "Provide a one-hour rated wall.", project="Project A",
            issue_tags=["fire_separation"], tag_status="probable",
        )
        index = ValidatedTagIndex([probable])
        self.assertEqual(index.exact("fire_separation"), [])

    def test_unconfirmed_global_tag_status_cannot_be_rule_inferred_into_stage_one(self):
        probable = row(
            "probable-global", "Provide a one-hour rated wall.",
            project="Project A", tag_status="probable",
        )
        index = ValidatedTagIndex([probable])
        self.assertEqual(index.exact("fire_separation"), [])

    def test_verified_event_with_probable_tag_is_not_exact(self):
        probable = row(
            "probable", "Provide a one-hour rated wall.", project="Project A",
            issue_tags=["fire_separation"], tag_status="probable",
        )
        result = progressive_retrieve("fire separation", [probable])
        self.assertNotEqual(result.stage, 1)
        self.assertEqual([item["comment_id"] for item in result.rows], ["probable"])

    def test_comparison_with_fewer_than_two_projects_is_not_sufficient(self):
        result = progressive_retrieve("fire separation", [self.fire_a], intent="compare_groups")
        self.assertFalse(result.coverage["project_count"] >= 2)

    def test_many_results_one_project_have_insufficient_coverage(self):
        rows = [copy.deepcopy(self.fire_a) for _ in range(5)]
        for index, item in enumerate(rows):
            item["comment_id"] = f"fire-{index}"
            item["canonical_event_id"] = f"event-{index}"
        result = progressive_retrieve("fire separation", rows, intent="compare_groups")
        self.assertEqual(result.stage, 3)
        self.assertEqual(result.coverage["project_count"], 1)

    def test_grading_record_cannot_support_fire_separation(self):
        result = progressive_retrieve("fire separation", [self.grading])
        self.assertEqual(result.rows, [])
        self.assertEqual(len(result.excluded), 1)
        self.assertIn("does not concern", result.excluded[0]["exclude_reason"])

    def test_duplicate_event_is_counted_once(self):
        duplicate = copy.deepcopy(self.fire_a)
        duplicate["comment_id"] = "fire-a-copy"
        duplicate["source_document"] = "copy.pdf"
        duplicate["source_occurrences"] = [{"source_document": "copy.pdf"}]
        result = progressive_retrieve("fire separation", [self.fire_a, duplicate])
        self.assertEqual(result.coverage["event_count"], 1)
        self.assertEqual(len(result.rows), 1)

    def test_source_occurrence_is_not_an_independent_comment(self):
        source_copy = copy.deepcopy(self.fire_a)
        source_copy["comment_id"] = "physical-copy"
        source_copy["canonical_event_id"] = self.fire_a["canonical_event_id"]
        source_copy["source_occurrences"] = [
            {"source_occurrence_id": "one"}, {"source_occurrence_id": "two"}
        ]
        result = progressive_retrieve("fire separation", [self.fire_a, source_copy])
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.coverage["event_count"], 1)

    def test_stage_three_new_discovery_is_only_suggested(self):
        # An explicit probable classification suppresses rule inference until
        # it is reviewed, forcing this record through the whole-corpus stage.
        untagged = row(
            "untagged", "Fire rated separation is required.", project="Project X",
            issue_tags=["fire_separation"], tag_status="probable",
        )
        result = progressive_retrieve("fire separation", [untagged], intent="precedent_search")
        self.assertEqual(result.stage, 3)
        self.assertTrue(result.suggested_tags)
        self.assertEqual(result.suggested_tags[0]["tag_status"], "suggested")

    def test_force_stage_three_bypasses_tag_first_retrieval(self):
        result = progressive_retrieve(
            "fire separation", [self.fire_a, self.fire_b], force_stage3=True,
        )
        self.assertEqual(result.stage, 3)
        self.assertEqual(result.fallback_reason, "forced_expand")

    def test_confirmed_response_count_is_backend_aggregation(self):
        result = progressive_retrieve("fire separation", [self.fire_a, self.fire_b])
        self.assertEqual(result.coverage["confirmed_response_count"], 2)

    def test_excluded_record_is_not_in_answer_rows(self):
        result = progressive_retrieve("fire separation", [self.fire_a, self.grading], intent="compare_groups")
        self.assertNotIn("grading", {item["comment_id"] for item in result.rows})
        self.assertIn("grading", {item["comment_id"] for item in result.excluded})

    def test_followup_scope_filter_is_applied(self):
        result = progressive_retrieve(
            "fire separation", [self.fire_a, self.fire_b],
            intent="compare_groups", filters={"city": "San Jose"},
        )
        self.assertEqual(result.coverage["project_count"], 2)
        result_filtered = progressive_retrieve(
            "fire separation", [self.fire_a, self.fire_b],
            intent="compare_groups", filters={"project_id": "Project B"},
        )
        self.assertEqual(result_filtered.coverage["project_count"], 1)
        self.assertEqual(result_filtered.rows[0]["comment_id"], "fire-b")

    def test_rebuilding_tag_index_does_not_change_canonical_rows(self):
        rows = [self.fire_a, self.fire_b]
        before = copy.deepcopy(rows)
        first = ValidatedTagIndex(rows)
        second = ValidatedTagIndex(rows)
        self.assertEqual(first.digest(), second.digest())
        self.assertTrue(all("target_ids" in entry for entry in first.as_dict()["entries"]))
        self.assertTrue(all("verified_text" not in entry for entry in first.as_dict()["entries"]))
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
