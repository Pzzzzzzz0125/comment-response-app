import unittest

from web_app.document_identity import canonicalize_documents, topic_occurrence_key, topic_occurrence_allowed


def row(path, text, *, sha="", city="Menlo Park", project="Project A", round_="1", comment_id=""):
    return {
        "comment_id": comment_id or "C-" + text[:8].replace(" ", "-"),
        "city": city,
        "property_project": project,
        "review_round": round_,
        "source_document": path,
        "source_sha256": sha,
        "original_text": text,
        "verified_text": text,
        "search_eligible": True,
    }


class DocumentIdentityTests(unittest.TestCase):
    def test_same_binary_in_two_folders_is_one_canonical_document(self):
        comments = [
            row("a/round-1.pdf", "Show fence height.", sha="same", round_="1", comment_id="C1"),
            row("b/round-2-copy.pdf", "Show fence height.", sha="same", round_="2", comment_id="C2"),
        ]
        result = canonicalize_documents(comments)
        self.assertEqual(result["physical_source_file_count"], 2)
        self.assertEqual(result["canonical_document_count"], 1)
        self.assertEqual(len(result["source_file_aliases"]), 1)
        self.assertEqual(comments[1]["occurrence_type"], "copied_duplicate")
        self.assertEqual(topic_occurrence_key(comments[0]), topic_occurrence_key(comments[1]))

    def test_normalized_reexport_and_blank_cover_are_one_document(self):
        comments = [
            row("original.pdf", "Show fence height.\n", sha="one", comment_id="C1"),
            row("archive/final.pdf", " Show   fence height. ", sha="two", comment_id="C2"),
        ]
        result = canonicalize_documents(comments)
        self.assertEqual(result["canonical_document_count"], 1)
        self.assertEqual(result["source_file_aliases"][0]["duplicate_reason"], "identical_normalized_content")

    def test_near_duplicate_is_quarantined_for_review(self):
        comments = [
            row("first.pdf", "Show the proposed fence height and label the height clearly on the architectural site plan for city review.", sha="one", comment_id="C1"),
            row("second.pdf", "Show the proposed fence height and label the height clearly on the architectural site plan for city review and coordination.", sha="two", comment_id="C2"),
        ]
        result = canonicalize_documents(comments)
        self.assertEqual(result["canonical_document_count"], 2)
        self.assertEqual(len(result["near_duplicate_review"]), 1)
        self.assertEqual(result["near_duplicate_review"][0]["review_status"], "needs_review")
        self.assertEqual({doc["duplicate_review_status"] for doc in result["canonical_documents"].values()}, {"needs_review"})

    def test_only_new_or_reissued_rows_contribute_to_topics(self):
        self.assertTrue(topic_occurrence_allowed({"occurrence_type": "newly_issued"}))
        self.assertTrue(topic_occurrence_allowed({"occurrence_type": "reissued_unresolved"}))
        self.assertTrue(topic_occurrence_allowed({}))
        self.assertFalse(topic_occurrence_allowed({"occurrence_type": "historical_quote"}))
        self.assertFalse(topic_occurrence_allowed({"occurrence_type": "copied_duplicate"}))

    def test_response_letter_quote_keeps_original_canonical_identity(self):
        text = "Provide tree protection measures on the plan."
        comments = [
            row("round-1/comments.pdf", text, sha="one", round_="1", comment_id="C-original"),
            row("round-2/Response Letter.pdf", text, sha="two", round_="2", comment_id="C-quote"),
            row("round-2/Response Letter.pdf", "Provide the new response note.", sha="two", round_="2", comment_id="C-new"),
        ]
        result = canonicalize_documents(comments)
        self.assertEqual(comments[1]["occurrence_type"], "historical_quote")
        self.assertEqual(comments[1]["carried_forward_from_comment_id"], "C-original")
        self.assertEqual(comments[1]["canonical_document_id"], comments[0]["canonical_document_id"])
        self.assertFalse(topic_occurrence_allowed(comments[1]))


if __name__ == "__main__":
    unittest.main()
